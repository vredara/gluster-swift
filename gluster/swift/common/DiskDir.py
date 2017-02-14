# Copyright (c) 2012-2013 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import stat
import errno

from gluster.swift.common.fs_utils import dir_empty, mkdirs, do_chown, \
    do_exists, do_touch, do_stat
from gluster.swift.common.utils import validate_account, validate_container, \
    get_container_details, get_account_details, create_container_metadata, \
    create_account_metadata, DEFAULT_GID, get_container_metadata, \
    get_account_metadata, DEFAULT_UID, validate_object, \
    create_object_metadata, read_metadata, write_metadata, X_CONTENT_TYPE, \
    X_CONTENT_LENGTH, X_TIMESTAMP, X_PUT_TIMESTAMP, X_ETAG, X_OBJECTS_COUNT, \
    X_BYTES_USED, X_CONTAINER_COUNT, DIR_TYPE, rmobjdir, dir_is_object, \
    list_objects_gsexpiring_container, normalize_timestamp
from gluster.swift.common import Glusterfs
from gluster.swift.common.exceptions import FileOrDirNotFoundError, \
    GlusterFileSystemIOError
from gluster.swift.obj.expirer import delete_tracker_object
from swift.common.constraints import MAX_META_COUNT, MAX_META_OVERALL_SIZE
from swift.common.swob import HTTPBadRequest
from gluster.swift.common.utils import ThreadPool
from docutils.nodes import container
from swift import account


DATADIR = 'containers'

# Create a dummy db_file in Glusterfs.RUN_DIR
_db_file = ""


def _read_metadata(dd):
    """ Filter read metadata so that it always returns a tuple that includes
        some kind of timestamp. With 1.4.8 of the Swift integration the
        timestamps were not stored. Here we fabricate timestamps for volumes
        where the existing data has no timestamp (that is, stored data is not
        a tuple), allowing us a measure of backward compatibility.

        FIXME: At this time it does not appear that the timestamps on each
        metadata are used for much, so this should not hurt anything.
    """
    metadata_i = read_metadata(dd)
    metadata = {}
    timestamp = 0
    for key, value in metadata_i.iteritems():
        if not isinstance(value, tuple):
            value = (value, timestamp)
        metadata[key] = value
    return metadata


def filter_prefix(objects, prefix):
    """
    Accept a sorted list of strings, returning all strings starting with the
    given prefix.
    """
    found = False
    for object_name in objects:
        if object_name.startswith(prefix):
            yield object_name
            found = True
        else:
            # Since the list is assumed to be sorted, once we find an object
            # name that does not start with the prefix we know we won't find
            # any others, so we exit early.
            if found:
                break


def filter_delimiter(objects, delimiter, prefix, marker, path=None):
    """
    Accept a sorted list of strings, returning strings that:
      1. begin with "prefix" (empty string matches all)
      2. does not match the "path" argument
      3. does not contain the delimiter in the given prefix length
    """
    assert delimiter
    assert prefix is not None
    skip_name = None
    for object_name in objects:
        if prefix and not object_name.startswith(prefix):
            break
        if path is not None:
            if object_name == path:
                continue
            if skip_name:
                if object_name < skip_name:
                    continue
                else:
                    skip_name = None
            end = object_name.find(delimiter, len(prefix))
            if end >= 0 and (len(object_name) > (end + 1)):
                skip_name = object_name[:end] + chr(ord(delimiter) + 1)
                continue
        else:
            if skip_name:
                if object_name < skip_name:
                    continue
                else:
                    skip_name = None
            end = object_name.find(delimiter, len(prefix))
            if end > 0:
                dir_name = object_name[:end + 1]
                if dir_name != marker:
                    yield dir_name
                skip_name = object_name[:end] + chr(ord(delimiter) + 1)
                continue
        yield object_name


def filter_marker(objects, marker):
    """
    Accept sorted list of strings, return all strings whose value is strictly
    greater than the given marker value.
    """
    for object_name in objects:
        if object_name > marker:
            yield object_name


def filter_prefix_as_marker(objects, prefix):
    """
    Accept sorted list of strings, return all strings whose value is greater
    than or equal to the given prefix value.
    """
    for object_name in objects:
        if object_name >= prefix:
            yield object_name


def filter_end_marker(objects, end_marker):
    """
    Accept a list of strings, sorted, and return all the strings that are
    strictly less than the given end_marker string. We perform this as a
    generator to avoid creating potentially large intermediate object lists.
    """
    for object_name in objects:
        if object_name < end_marker:
            yield object_name
        else:
            break


class DiskCommon(object):
    """
    Common fields and methods shared between DiskDir and DiskAccount classes.
    """
    def __init__(self, root, drive, account, logger, pending_timeout=None,
                 stale_reads_ok=False):
        # WARNING: The following four fields are referenced as fields by our
        # callers outside of this module, do not remove.
        # Create a dummy db_file in Glusterfs.RUN_DIR
        global _db_file
        if not _db_file:
            _db_file = os.path.join(Glusterfs.RUN_DIR, 'db_file.db')
            if not do_exists(_db_file):
                do_touch(_db_file)
        self.db_file = _db_file
        self.metadata = {}
        self.pending_timeout = pending_timeout or 10
        self.stale_reads_ok = stale_reads_ok
        # The following fields are common
        self.root = root
        assert logger is not None
        self.logger = logger
        self.account = account
        self.datadir = os.path.join(root, drive)
        self._dir_exists = False

        # nthread=0 is intentional. This ensures that no green pool is
        # used. Call to force_run_in_thread() will ensure that the method
        # passed as arg is run in a real external thread using eventlet.tpool
        # which has a threadpool of 20 threads (default)
        self.threadpool = ThreadPool(nthreads=0)

    def _dir_exists_read_metadata(self):
        self._dir_exists = os.path.isdir(self.datadir)
        if self._dir_exists:
            try:
                self.metadata = _read_metadata(self.datadir)
            except GlusterFileSystemIOError as err:
                if err.errno in (errno.ENOENT, errno.ESTALE):
                    return False
                raise
        return self._dir_exists

    def is_deleted(self):
        # The intention of this method is to check the file system to see if
        # the directory actually exists.
        return not self._dir_exists

    def empty(self):
        # If it does not exist, then it is empty.  A value of True is
        # what is expected by OpenStack Swift when the directory does
        # not exist.  Check swift/common/db.py:ContainerBroker.empty()
        # and swift/container/server.py:ContainerController.DELETE()
        # for more information
        try:
            return dir_empty(self.datadir)
        except FileOrDirNotFoundError:
            return True

    def validate_metadata(self, metadata):
        """
        Validates that metadata falls within acceptable limits.

        :param metadata: to be validated
        :raises: HTTPBadRequest if MAX_META_COUNT or MAX_META_OVERALL_SIZE
                 is exceeded
        """
        meta_count = 0
        meta_size = 0
        for key, (value, timestamp) in metadata.iteritems():
            key = key.lower()
            if value != '' and (key.startswith('x-account-meta') or
                                key.startswith('x-container-meta')):
                prefix = 'x-account-meta-'
                if key.startswith('x-container-meta-'):
                    prefix = 'x-container-meta-'
                key = key[len(prefix):]
                meta_count = meta_count + 1
                meta_size = meta_size + len(key) + len(value)
        if meta_count > MAX_META_COUNT:
            raise HTTPBadRequest('Too many metadata items; max %d'
                                 % MAX_META_COUNT)
        if meta_size > MAX_META_OVERALL_SIZE:
            raise HTTPBadRequest('Total metadata too large; max %d'
                                 % MAX_META_OVERALL_SIZE)

    def update_metadata(self, metadata, validate_metadata=False):
        assert self.metadata, "Valid container/account metadata should have " \
            "been created by now"
        if metadata:
            new_metadata = self.metadata.copy()
            new_metadata.update(metadata)
            if validate_metadata:
                self.validate_metadata(new_metadata)
            if new_metadata != self.metadata:
                write_metadata(self.datadir, new_metadata)
                self.metadata = new_metadata


class DiskDir(DiskCommon):
    """
    Manage object files on disk.

    :param path: path to devices on the node
    :param drive: gluster volume drive name
    :param account: account name for the object
    :param container: container name for the object
    :param logger: account or container server logging object
    :param uid: user ID container object should assume
    :param gid: group ID container object should assume

    Usage pattern from container/server.py (Kilo, 2.3.0):
        DELETE:
            if auto-create and obj and not .db_file:
                # Creates container
                .initialize()
            if not .db_file:
                # Container does not exist
                return 404
            if obj:
                # Should be a NOOP
                .delete_object()
            else:
                if not .empty()
                    # Gluster's definition of empty should mean only
                    # sub-directories exist in Object-Only mode
                    return conflict
                .get_info()['put_timestamp'] and not .is_deleted()
                # Deletes container
                .delete_db()
                if not .is_deleted():
                    return conflict
                account_update():
                    .get_info()
        PUT:
            if obj:
                if auto-create cont and not .db_file
                    # Creates container
                    .initialize()
                if not .db_file
                    return 404
                .put_object()
            else:
                _update_or_create():
                    if not .db_file:
                        # Creates container
                        .initialize()
                    recreated = .is_deleted():
                    if recreated:
                        .set_storage_policy_index()
                    .storage_policy_index
                    .update_put_timestamp()
                    if .is_deleted()
                        return conflict
                    if recreated:
                        .update_status_changed_at()

                if 'X-Container-Sync-To' in metadata:
                    if .metadata
                        .set_x_container_sync_points()
                    .update_metadata()
                account_update():
                    .get_info()
        HEAD:
            info, is_deleted = .get_info_is_deleted()
            .get_info_is_deleted():
                if not .db_file:
                    return {}, True
                info = .get_info()
                return info, ._is_deleted_info()
            .metadata
        GET:
            info, is_deleted = .get_info_is_deleted()
            .get_info_is_deleted():
                if not .db_file:
                    return {}, True
                info = .get_info()
                return info, ._is_deleted_info()
            .list_objects_iter()
            .metadata
        POST:
            if .is_deleted():
                return 404
            .metadata
            .set_x_container_sync_points()
            .update_metadata()
    """

    def __init__(self, path, drive, account, container, logger,
                 uid=DEFAULT_UID, gid=DEFAULT_GID, **kwargs):
        super(DiskDir, self).__init__(path, drive, account, logger, **kwargs)

        self.uid = int(uid)
        self.gid = int(gid)

        self.container = container
        self.datadir = os.path.join(self.datadir, self.container)
        
        if self.account == 'gsexpiring':
            # Do not bother crawling the entire container tree just to update
            # object count and bytes used. Return immediately before metadata
            # validation and creation happens.
            info = do_stat(self.datadir)
            if info and stat.S_ISDIR(info.st_mode):
                self._dir_exists = True
            if not info:
                # Container no longer exists.
                return
            semi_fake_md = {
                'X-Object-Count': (0, 0),
                'X-Timestamp': ((normalize_timestamp(info.st_ctime)), 0),
                'X-Type': ('container', 0),
                'X-PUT-Timestamp': ((normalize_timestamp(info.st_mtime)), 0),
                'X-Bytes-Used': (0, 0)
            }
            self.metadata = semi_fake_md
            return

        if not self._dir_exists_read_metadata():
            return

        if not self.metadata:
            create_container_metadata(self.datadir)
            self.metadata = _read_metadata(self.datadir)
        else:
            if not validate_container(self.metadata):
                create_container_metadata(self.datadir)
                self.metadata = _read_metadata(self.datadir)

    def update_status_changed_at(self, timestamp):
        return

    @property
    def storage_policy_index(self):
        if not hasattr(self, '_storage_policy_index'):
            self._storage_policy_index = \
                self.get_info()['storage_policy_index']
        return self._storage_policy_index

    def set_storage_policy_index(self, policy_index, timestamp=None):
        self._storage_policy_index = policy_index

    def list_objects_iter(self, limit, marker, end_marker,
                          prefix, delimiter, path=None,
                          storage_policy_index=0,
                          out_content_type=None,reverse=False):
        """
        Returns tuple of name, created_at, size, content_type, etag.
        """
        assert limit >= 0
        assert not delimiter or (len(delimiter) == 1 and ord(delimiter) <= 254)

        if path is not None:
            if path:
                prefix = path = path.rstrip('/') + '/'
            else:
                prefix = path
            delimiter = '/'
        elif delimiter and not prefix:
            prefix = ''

        container_list = []

        if self.account == 'gsexpiring':
            objects = list_objects_gsexpiring_container(self.datadir)
        else:
            objects = self._update_object_count()
        if objects:
            objects.sort()
        else:
            # No objects in container , return empty list
            return container_list
        
        if marker and end_marker and reverse:
            marker,end_marker = end_marker,marker
            
        if end_marker:
            objects = filter_end_marker(objects, end_marker)

        if marker and marker >= prefix:
            objects = filter_marker(objects, marker)
        elif prefix:
            objects = filter_prefix_as_marker(objects, prefix)
            
        if prefix is None:
            # No prefix, we don't need to apply the other arguments, we just
            # return what we have.
            pass
        else:
            # We have a non-None (for all intents and purposes it is a string)
            # prefix.
            if not delimiter:
                if not prefix:
                    # We have nothing more to do
                    pass
                else:
                    objects = filter_prefix(objects, prefix)
            else:
                objects = filter_delimiter(objects, delimiter, prefix, marker,
                                           path)
                
        if out_content_type == 'text/plain' or \
                self.account == 'gsexpiring':
            # When out_content_type == 'text/plain':
            #
            # The client is only asking for a plain list of objects and NOT
            # asking for any extended information about objects such as
            # bytes used or etag.
            #
            # When self.account == 'gsexpiring':
            #
            # This is a JSON request sent by the object expirer to list
            # tracker objects in a container in gsexpiring volume.
            # When out_content_type is 'application/json', the caller
            # expects each record entry to have the following ordered
            # fields: (name, timestamp, size, content_type, etag)
            for obj in objects:
                container_list.append((obj, '0', 0, 'text/plain', ''))
                if len(container_list) >= limit:
                        break
            if reverse:
                container_list.reverse()
            return container_list

        count = 0
        for obj in objects:
            obj_path = os.path.join(self.datadir, obj)
            try:
                metadata = read_metadata(obj_path)
            except GlusterFileSystemIOError as err:
                if err.errno in (errno.ENOENT, errno.ESTALE):
                    # obj might have been deleted by another process
                    # since the objects list was originally built
                    continue
                else:
                    raise err
            if not metadata or not validate_object(metadata):
                if delimiter == '/' and obj_path[-1] == delimiter:
                    clean_obj_path = obj_path[:-1]
                else:
                    clean_obj_path = obj_path
                try:
                    metadata = create_object_metadata(clean_obj_path)
                except OSError as e:
                    # FIXME - total hack to get upstream swift ported unit
                    # test cases working for now.
                    if e.errno not in (errno.ENOENT, errno.ESTALE):
                        raise
            if not Glusterfs._implicit_dir_objects and metadata \
                    and metadata[X_CONTENT_TYPE] == DIR_TYPE \
                    and not dir_is_object(metadata):
                continue
            list_item = []
            list_item.append(obj)
            if metadata:
                list_item.append(metadata[X_TIMESTAMP])
                list_item.append(int(metadata[X_CONTENT_LENGTH]))
                list_item.append(metadata[X_CONTENT_TYPE])
                list_item.append(metadata[X_ETAG])
            container_list.append(list_item)
            count += 1
            if count >= limit:
                break
        if reverse:
            container_list.reverse()    
        return container_list

    def _update_object_count(self):
        objects, object_count, bytes_used = get_container_details(self.datadir)

        if X_OBJECTS_COUNT not in self.metadata \
                or int(self.metadata[X_OBJECTS_COUNT][0]) != object_count \
                or X_BYTES_USED not in self.metadata \
                or int(self.metadata[X_BYTES_USED][0]) != bytes_used:
            self.metadata[X_OBJECTS_COUNT] = (object_count, 0)
            self.metadata[X_BYTES_USED] = (bytes_used, 0)
            write_metadata(self.datadir, self.metadata)

        return objects

    def get_info_is_deleted(self):
        if not self._dir_exists:
            return {}, True
        info = self.get_info()
        return info, False

    def get_info(self):
        """
        Get global data for the container.
        :returns: dict with keys: account, container, object_count, bytes_used,
                      hash, id, created_at, put_timestamp, delete_timestamp,
                      reported_put_timestamp, reported_delete_timestamp,
                      reported_object_count, and reported_bytes_used.
        """
        if self._dir_exists and Glusterfs._container_update_object_count and \
                self.account != 'gsexpiring':
            self._update_object_count()

        data = {'account': self.account, 'container': self.container,
                'object_count': self.metadata.get(
                    X_OBJECTS_COUNT, ('0', 0))[0],
                'bytes_used': self.metadata.get(X_BYTES_USED, ('0', 0))[0],
                'hash': '', 'id': '', 'created_at': '1',
                'put_timestamp': self.metadata.get(
                    X_PUT_TIMESTAMP, ('0', 0))[0],
                'delete_timestamp': '1',
                'reported_put_timestamp': '1',
                'reported_delete_timestamp': '1',
                'reported_object_count': '1', 'reported_bytes_used': '1',
                'x_container_sync_point1': self.metadata.get(
                    'x_container_sync_point1', -1),
                'x_container_sync_point2': self.metadata.get(
                    'x_container_sync_point2', -1),
                'storage_policy_index': self.metadata.get(
                    'storage_policy_index', 0)
                }
        self._storage_policy_index = data['storage_policy_index']
        return data

    def put_object(self, name, timestamp, size, content_type, etag, deleted=0):
        # NOOP - should never be called since object file creation occurs
        # within a directory implicitly.
        pass

    def initialize(self, timestamp):
        """
        Create and write metatdata to directory/container.
        :param metadata: Metadata to write.
        """
        if not self._dir_exists:
            mkdirs(self.datadir)
            # If we create it, ensure we own it.
            do_chown(self.datadir, self.uid, self.gid)
        metadata = get_container_metadata(self.datadir)
        metadata[X_TIMESTAMP] = (timestamp, 0)
        write_metadata(self.datadir, metadata)
        self.metadata = metadata
        self._dir_exists = True

    def update_put_timestamp(self, timestamp):
        """
        Update the PUT timestamp for the container.

        If the container does not exist, create it using a PUT timestamp of
        the given value.

        If the container does exist, update the PUT timestamp only if it is
        later than the existing value.
        """
        if not do_exists(self.datadir):
            self.initialize(timestamp)
        else:
            if timestamp > self.metadata[X_PUT_TIMESTAMP]:
                self.metadata[X_PUT_TIMESTAMP] = (timestamp, 0)
                write_metadata(self.datadir, self.metadata)

    def delete_object(self, name, timestamp, obj_policy_index):
        if self.account == 'gsexpiring':
            # The request originated from object expirer. This should
            # delete tracker object.
            self.threadpool.force_run_in_thread(delete_tracker_object,
                                                self.datadir, name)
        else:
            # NOOP - should never be called since object file removal occurs
            # within a directory implicitly.
            return

    def delete_db(self, timestamp):
        """
        Delete the container (directory) if empty.

        :param timestamp: delete timestamp
        """
        # Let's check and see if it has directories that
        # where created by the code, but not by the
        # caller as objects
        rmobjdir(self.datadir)
        self._dir_exists = False

    def set_x_container_sync_points(self, sync_point1, sync_point2):
        self.metadata['x_container_sync_point1'] = sync_point1
        self.metadata['x_container_sync_point2'] = sync_point2


class DiskAccount(DiskCommon):
    """
    Usage pattern from account/server.py (Kilo, 2.3.0):
        DELETE:
            .is_deleted()
            .is_status_deleted()
            .delete_db()
            .is_status_deleted()
        PUT:
            container:
                .db_file
                .initialize()
                .is_deleted()
                .put_container()
            account:
                .db_file
                .initialize()
                .is_status_deleted()
                .is_status_deleted()
                .is_deleted()
                .update_put_timestamp()
                .is_deleted()
                .update_metadata()
        HEAD:
            .is_deleted()
            .is_status_deleted()
            .get_info()
            .get_policy_stats()
            .metadata
        GET:
            .is_deleted()
            .is_status_deleted()
            .get_info()
            .get_policy_stats()
            .metadata
            .list_containers_iter()
        POST:
            .is_deleted()
            .is_status_deleted()
            .update_metadata()
    """

    def __init__(self, root, drive, account, logger, **kwargs):
        super(DiskAccount, self).__init__(root, drive, account, logger,
                                          **kwargs)

        if self.account == 'gsexpiring':
            # Do not bother updating object count, container count and bytes
            # used. Return immediately before metadata validation and
            # creation happens.
            info = do_stat(self.datadir)
            if info and stat.S_ISDIR(info.st_mode):
                self._dir_exists = True
            semi_fake_md = {
                'X-Object-Count': (0, 0),
                'X-Container-Count': (0, 0),
                'X-Timestamp': ((normalize_timestamp(info.st_ctime)), 0),
                'X-Type': ('Account', 0),
                'X-PUT-Timestamp': ((normalize_timestamp(info.st_mtime)), 0),
                'X-Bytes-Used': (0, 0)
            }
            self.metadata = semi_fake_md
            return

        # Since accounts should always exist (given an account maps to a
        # gluster volume directly, and the mount has already been checked at
        # the beginning of the REST API handling), just assert that that
        # assumption still holds.
        assert self._dir_exists_read_metadata()
        assert self._dir_exists

        if not self.metadata or not validate_account(self.metadata):
            create_account_metadata(self.datadir)
            self.metadata = _read_metadata(self.datadir)

    def is_status_deleted(self):
        """
        Only returns true if the status field is set to DELETED.
        """
        # This function should always return False. Accounts are not created
        # and deleted, they exist if a Gluster volume can be mounted. There is
        # no way to delete accounts, so this could never return True.
        return False

    def initialize(self, timestamp):
        """
        Create and write metatdata to directory/account.
        :param metadata: Metadata to write.
        """
        metadata = get_account_metadata(self.datadir)
        metadata[X_TIMESTAMP] = (timestamp, 0)
        write_metadata(self.datadir, metadata)
        self.metadata = metadata

    def update_put_timestamp(self, timestamp):
        # Since accounts always exists at this point, just update the account
        # PUT timestamp if this given timestamp is later than what we already
        # know.
        assert self._dir_exists

        if timestamp > self.metadata[X_PUT_TIMESTAMP][0]:
            self.metadata[X_PUT_TIMESTAMP] = (timestamp, 0)
            write_metadata(self.datadir, self.metadata)

    def delete_db(self, timestamp):
        """
        Mark the account as deleted

        :param timestamp: delete timestamp
        """
        # Deleting an account is a no-op, since accounts are one-to-one
        # mappings to gluster volumes.
        #
        # FIXME: This means the caller will end up returning a success status
        # code for an operation that really should not be allowed. Instead, we
        # should modify the account server to not allow the DELETE method, and
        # should probably modify the proxy account controller to not allow the
        # DELETE method as well.
        return

    def put_container(self, container, put_timestamp, del_timestamp,
                      object_count, bytes_used):
        """
        Create a container with the given attributes.

        :param name: name of the container to create
        :param put_timestamp: put_timestamp of the container to create
        :param delete_timestamp: delete_timestamp of the container to create
        :param object_count: number of objects in the container
        :param bytes_used: number of bytes used by the container
        """
        # NOOP - should never be called since container directory creation
        # occurs from within the account directory implicitly.
        return

    def _update_container_count(self):
        containers, container_count = get_account_details(self.datadir)

        if X_CONTAINER_COUNT not in self.metadata \
                or int(self.metadata[X_CONTAINER_COUNT][0]) != container_count:
            self.metadata[X_CONTAINER_COUNT] = (container_count, 0)
            write_metadata(self.datadir, self.metadata)

        return containers

    def list_containers_iter(self, limit, marker, end_marker,
                             prefix, delimiter, response_content_type=None,reverse=False):
        """
        Return tuple of name, object_count, bytes_used, 0(is_subdir).
        Used by account server.
        """
        if delimiter and not prefix:
            prefix = ''

        account_list = []
        containers = self._update_container_count()
        if containers:
            containers.sort()
        else:
            # No containers in account, return empty list
            return account_list
        
        if marker and end_marker and reverse:
            marker,end_marker = end_marker,marker
            
        if containers and end_marker:
            containers = filter_end_marker(containers, end_marker)

        if containers:
            if marker and marker >= prefix:
                containers = filter_marker(containers, marker)
            elif prefix:
                containers = filter_prefix_as_marker(containers, prefix)

        if prefix is None:
            # No prefix, we don't need to apply the other arguments, we just
            # return what we have.
            pass
        else:
            # We have a non-None (for all intents and purposes it is a string)
            # prefix.
            if not delimiter:
                if not prefix:
                    # We have nothing more to do
                    pass
                else:
                    containers = filter_prefix(containers, prefix)
            else:
                containers = filter_delimiter(containers, delimiter, prefix,
                                              marker)

        if response_content_type == 'text/plain' or \
                self.account == 'gsexpiring':
            # When response_content_type == 'text/plain':
            #
            # The client is only asking for a plain list of containers and NOT
            # asking for any extended information about container such as
            # bytes used or object count.
            #
            # When self.account == 'gsexpiring':
            # This is a JSON request sent by the object expirer to list
            # containers in gsexpiring volume. When out_content_type is
            # 'application/json', the caller expects each record entry to have
            # the following ordered fields:
            # (name, object_count, bytes_used, is_subdir)
            for container in containers:
                # When response_content_type == 'text/plain', Swift will only
                # consume the name of the container (first element of tuple).
                # Refer: swift.account.utils.account_listing_response()
                account_list.append((container, 0, 0, 0))
                if len(account_list) >= limit:
                    break
            if reverse:
                account_list.reverse()
            return account_list

        count = 0
        for cont in containers:
            list_item = []
            metadata = None
            list_item.append(cont)
            cont_path = os.path.join(self.datadir, cont)
            metadata = _read_metadata(cont_path)
            if not metadata or not validate_container(metadata):
                try:
                    metadata = create_container_metadata(cont_path)
                except OSError as e:
                    # FIXME - total hack to get upstream swift ported unit
                    # test cases working for now.
                    if e.errno not in (errno.ENOENT, errno.ESTALE):
                        raise
            if metadata:
                list_item.append(metadata[X_OBJECTS_COUNT][0])
                list_item.append(metadata[X_BYTES_USED][0])
                list_item.append(0)
            account_list.append(list_item)
            count += 1
            if count >= limit:
                break
        if reverse:
            account_list.reverse()
        return account_list

    def get_info(self):
        """
        Get global data for the account.
        :returns: dict with keys: account, created_at, put_timestamp,
                  delete_timestamp, container_count, object_count,
                  bytes_used, hash, id
        """
        if Glusterfs._account_update_container_count and \
                self.account != 'gsexpiring':
            self._update_container_count()

        data = {'account': self.account, 'created_at': '1',
                'put_timestamp': '1', 'delete_timestamp': '1',
                'container_count': self.metadata.get(
                    X_CONTAINER_COUNT, (0, 0))[0],
                'object_count': self.metadata.get(X_OBJECTS_COUNT, (0, 0))[0],
                'bytes_used': self.metadata.get(X_BYTES_USED, (0, 0))[0],
                'hash': '', 'id': ''}
        return data

    def get_policy_stats(self, do_migrations=False):
        return {}
