import time
import logging
import threading
import hashlib
from ssl import SSLError

import arrow
from apiclient.discovery import build   # pylint: disable=import-error
from apiclient.errors import HttpError  # pylint: disable=import-error
from httplib2 import Http, HttpLib2Error
from oauth2client import client         # pylint: disable=import-error
from oauth2client.client import HttpAccessTokenRefreshError # pylint: disable=import-error

from apiclient.http import MediaIoBaseDownload, MediaIoBaseUpload # pylint: disable=import-error

from cloudsync import Provider, ProviderInfo, DIRECTORY, FILE, Event

from cloudsync.exceptions import CloudTokenError, CloudDisconnectedError, CloudFileNotFoundError, CloudTemporaryError
from cloudsync.runnable import time_helper

log = logging.getLogger(__name__)

class GDriveInfo(ProviderInfo):
    pids = []
    name = ""
    def __new__(cls, *a, pids=[], name=None):
        self = super().__new__(cls, *a)
        self.pids = pids
        self.name = name
        return self

class GDriveProvider(Provider):         # pylint: disable=too-many-public-methods
    case_sensitive = False
    allow_renames_over_existing = False
    require_parent_folder = True

    _scope = "https://www.googleapis.com/auth/drive"
    _redir = 'urn:ietf:wg:oauth:2.0:oob'
    _token_uri = 'https://accounts.google.com/o/oauth2/token'
    _folder_mime_type = 'application/vnd.google-apps.folder'

    def __init__(self, sync_root):
        super().__init__(sync_root)
        self.__root_id = None
        self.__sync_root_id = None
        self.__cursor = None
        self.client = None
        self.api_key = None
        self.refresh_token = None
        self.user_agent = 'cloudsync/1.0'
        self.mutex = threading.Lock()
        self._ids = {"/":"root"}

    @property
    def connected(self):
        return self.client is not None

    def get_quota(self):
        # https://developers.google.com/drive/api/v3/reference/about
        res = self._api('about', 'get', fields='storageQuota,user')

        quota = res['storageQuota']
        user = res['user']

        usage = int(quota['usage'])
        if 'limit' in quota and quota['limit']:
            limit = int(quota['limit'])
        else:
            # It is possible for an account to have unlimited space - pretend it's 1TB
            limit = 1024 * 1024 * 1024 * 1024

        res = {
            'used': usage,
            'total': limit,
            'login': user['emailAddress'],
            'uid': user['permissionId']
        }

        return res

    def connect(self, creds):
        log.debug('Connecting to googledrive')
        if not self.client:
            api_key = creds.get('api_key', self.api_key)
            refresh_token = creds.get('refresh_token', self.refresh_token)
            kwargs = {}
            try:
                with self.mutex:
                    creds = client.GoogleCredentials(access_token=api_key,
                                                     client_id=creds.get(
                                                         'client_id'),
                                                     client_secret=creds.get(
                                                         'client_secret'),
                                                     refresh_token=refresh_token,
                                                     token_expiry=None,
                                                     token_uri=self._token_uri,
                                                     user_agent=self.user_agent)
                    creds.refresh(Http())
                    self.client = build(
                        'drive', 'v3', http=creds.authorize(Http()))
                    kwargs['api_key'] = creds.access_token

                if getattr(creds, 'refresh_token', None):
                    refresh_token = creds.refresh_token

                self.refresh_token = refresh_token
                self.api_key = api_key

                try:
                    self.get_quota()
                except SSLError:  # pragma: no cover
                    # Seeing some intermittent SSL failures that resolve on retry
                    log.warning('Retrying intermittent SSLError')
                    self.get_quota()
            except HttpAccessTokenRefreshError:
                self.disconnect()
                raise CloudTokenError()

        if not self.sync_root_id:
            raise CloudFileNotFoundError("cant create sync root")

        return self.client

    def _api(self, resource, method, *args, **kwargs):          # pylint: disable=arguments-differ
        if not self.client:
            raise CloudDisconnectedError("currently disconnected")

        with self.mutex:
            try:
                res = getattr(self.client, resource)()
                meth = getattr(res, method)(*args, **kwargs)
                return meth.execute()
            except HttpAccessTokenRefreshError:
                self.disconnect()
                raise CloudTokenError()
            except HttpError as e:
                if str(e.resp.status) == '404':
                    raise CloudFileNotFoundError('File not found when executing %s.%s(%s)' % (
                        resource, method, kwargs
                    ))
                if (str(e.resp.status) == '403' and str(e.resp.reason) == 'Forbidden') or str(e.resp.status) == '429':
                    raise CloudTemporaryError("rate limit hit")
                
                raise CloudTemporaryError("unknown error %s" % e)
            except (TimeoutError, HttpLib2Error) as e:
                self.disconnect()
                raise CloudDisconnectedError("disconnected on timeout")

    @property
    def root_id(self):
        if not self.__root_id:
            res = self._api('files', 'get',
                    fileId='root',
                    fields='id',
                    )
            self.__root_id = res['id']
            self._ids['/'] = self.__root_id
        return self.__root_id

    @property
    def sync_root_id(self):
        if not self.__sync_root_id:
            if not self.info_path(self.sync_root):
                self.mkdir(self.sync_root)
            if not self.info_path(self.sync_root):
                raise CloudFileNotFoundError("Cannot create sync root")
            info = self.info_path(self.sync_root)
            self.__sync_root_id = info.oid

        return self.__sync_root_id

    def disconnect(self):
        self.client = None

    @property
    def cursor(self):
        if not self.__cursor:
            res = self._api('changes', 'getStartPageToken')
            self.__cursor = res.get('startPageToken')
        return self.__cursor

    def is_suboid(self, top, oid):
        if top == oid:
            return True
        pid = self.get_parent_id(oid)
        if pid == oid:
            return False
        return self.is_suboid(top, pid)

    def events(self, timeout):      # pylint: disable=too-many-locals
        got_something = False
        for _ in time_helper(timeout, sleep=3):
            page_token = self.cursor
            while page_token is not None:
#                log.debug("looking for events, timeout: %s", timeout)
                response = self._api('changes', 'list', pageToken=page_token, spaces='drive', includeRemoved=True, 
                        includeItemsFromAllDrives=True, supportsAllDrives=True)
                for change in response.get('changes'):
                    log.debug("got event %s", change)

                    # {'kind': 'drive#change', 'type': 'file', 'changeType': 'file', 'time': '2019-07-23T16:57:06.779Z', 
                    # 'removed': False, 'fileId': '1NCi2j1SjsPUTQTtaD2dFNsrt49J8TPDd', 'file': {'kind': 'drive#file', 
                    # 'id': '1NCi2j1SjsPUTQTtaD2dFNsrt49J8TPDd', 'name': 'dest', 'mimeType': 'application/octet-stream'}}

                    # {'kind': 'drive#change', 'type': 'file', 'changeType': 'file', 'time': '2019-07-23T20:02:14.156Z', 
                    # 'removed': True, 'fileId': '1lhRe0nDplA6I5JS18642rg0KIbYN66lR'}

                    ts = arrow.get(change.get('time')).float_timestamp
                    oid = change.get('fileId')
                    exists = not change.get('removed') 
 
                    fil = change.get('file')
                    if fil:
                        if fil.get('mimeType') == self._folder_mime_type:
                            otype = DIRECTORY
                        else:
                            otype = FILE
                    else:
                        otype = None

                    ohash = None
                    path = self._path_oid(oid)

                    if path:
                        if not self.is_subpath(self.sync_root, path):
                            log.debug("skipped event %s as %s", self.sync_root, path)
                            continue
                    else:
                        if not self.is_suboid(self.sync_root_id, oid):
                            log.debug("skipped event %s", change)
                            continue

                    event = Event(otype, oid, path, ohash, exists, ts)

                    log.debug("converted event %s as %s", change, event)

                    yield event

                    got_something = True
                page_token = response.get('nextPageToken')
                if 'newStartPageToken' in response:
                    self.__cursor = response.get('newStartPageToken')
            if got_something:
                break

    def walk(self, since=None):
        ...

    def upload(self, oid, file_like, metadata=None):
        if not metadata:
            metadata = {} 
        gdrive_info = self.__prep_upload(None, metadata)

        ul = MediaIoBaseUpload(file_like, mimetype=gdrive_info.get('mimeType'), chunksize=4 * 1024 * 1024)

        fields = 'id, md5Checksum'

        res = self._api('files', 'update',
                body=gdrive_info,
                fileId=oid,
                media_body=ul,
                fields=fields)

        log.debug("response from upload %s", res)

        if not res:
            raise CloudTemporaryError("unknown response from drive on upload")

        return ProviderInfo(oid=res['id'], hash=res['md5Checksum'], path=None)

    def __prep_upload(self, path, metadata):
        # modification time
        mtime = metadata.get("modifiedTime", time.time())
        mtime = arrow.get(mtime).isoformat()
        gdrive_info = {
                'modifiedTime':  mtime
                }

        # mime type, if provided
        mime_type = metadata.get("mimeType", "application/octet-stream")
        if mime_type:
            gdrive_info['mimeType'] = mime_type

        # path, if provided
        if path:
            _, name = self.split(path)
            gdrive_info['name'] = name

        # misc properties, if provided
        app_props = metadata.get("appProperties", None)
        if app_props:
            # caller can specify google-specific stuff, if desired
            gdrive_info['appProperties'] = app_props

        # misc properties, if provided
        app_props = metadata.get("properties", None)
        if app_props:
            # caller can specify google-specific stuff, if desired
            gdrive_info['properties'] = app_props

        log.debug("info %s", gdrive_info)

        return gdrive_info

    def create(self, path, file_like, metadata=None) -> 'ProviderInfo':
        if not metadata:
            metadata = {} 
        gdrive_info = self.__prep_upload(path, metadata)

        ul = MediaIoBaseUpload(file_like, mimetype=gdrive_info.get('mimeType'), chunksize=4 * 1024 * 1024)

        fields = 'id, md5Checksum'

        parent_oid = self.get_parent_id(path)

        gdrive_info['parents'] = [parent_oid]

        res = self._api('files', 'create',
                body=gdrive_info,
                media_body=ul,
                fields=fields)

        log.debug("response from create %s : %s", path, res)

        if not res:
            raise CloudTemporaryError("unknown response from drive on upload")

        self._ids[path] = res['id']

        log.debug("path cache %s", self._ids)

        return ProviderInfo(oid=res['id'], hash=res['md5Checksum'], path=path)

    def download(self, oid, file_like):
        req = self.client.files().get_media(fileId=oid)
        dl = MediaIoBaseDownload(file_like, req, chunksize=4 * 1024 * 1024)

        done = False
        while not done:
            try:
                _, done = dl.next_chunk()
            except HttpError as e:
                if str(e.resp.status) == '416':
                    log.debug("empty file downloaded")
                    done = True
                elif str(e.resp.status) == '404':
                    raise CloudFileNotFoundError("file %s not found" % oid) 
                else:
                    raise CloudTemporaryError("unknown response from drive")
            except (TimeoutError, HttpLib2Error) as e:
                self.disconnect()
                raise CloudDisconnectedError("disconnected during download")

    def rename(self, oid, path):
        pid = self.get_parent_id(path)

        add_pids = [pid]
        if pid == 'root':
            add_pids = [self.root_id]

        info = self._info_oid(oid)
        remove_pids = info.pids

        _, name = self.split(path)
        body = {'name': name}

        self._api('files', 'update', body=body, fileId=oid, addParents=add_pids, removeParents=remove_pids, fields='id')

        for cpath, coid in list(self._ids.items()):
            if coid == oid:
                self._ids.pop(cpath)
                self._ids[path] = oid

        log.debug("renamed %s", body)

    def listdir(self, oid) -> list:
        query = f"'{oid}' in parents"
        try:
            res = self._api('files', 'list',
                    q=query,
                    spaces='drive',
                    fields='files(id, md5Checksum, parents, name, trashed)',
                    pageToken=None)
        except CloudFileNotFoundError:
            if self._info_oid(oid):
                return []
            log.debug("OID GONE %s", oid)
            raise

        if not res:
            return []


        log.debug("got res %s", res)

        ret = []
        for ent in res['files']:
            fid = ent['id']
            pids = ent['parents']
            fhash = ent['md5Checksum']
            name = ent['name']
            trashed = ent.get('trashed', False)
            if not trashed:
                ret.append(GDriveInfo(fid, fhash, None, pids=pids, name=name))

        log.debug("listdir %s", ret)
        return ret
 
    def mkdir(self, path, metadata=None) -> str:    # pylint: disable=arguments-differ
        pid = self.get_parent_id(path)
        _, name = self.split(path)
        file_metadata = {
            'name': name,
            'parents': [pid],
            'mimeType': self._folder_mime_type,
        }
        if metadata:
            file_metadata.update(metadata)
        res = self._api('files', 'create',
                body=file_metadata, fields='id')
        fileid = res.get('id')
        self._ids[path] = fileid

    def delete(self, oid):
        self._api('files', 'delete', fileId=oid)

    def exists_oid(self, oid):
        return self._info_oid(oid)

    def info_path(self, path) -> ProviderInfo:
        parent_id = self.get_parent_id(path)
        _, name = self.split(path)

        query = f"'{parent_id}' in parents and name='{name}'"

        try:
            res = self._api('files', 'list',
                    q=query,
                    spaces='drive',
                    fields='files(id, md5Checksum, parents)',
                    pageToken=None)
        except CloudFileNotFoundError:
            return None

        if not res['files']:
            return None

        ent = res['files'][0]

        oid = ent['id']
        pids = ent['parents']
        fhash = ent.get('md5Checksum')

        self._ids[path] = oid

        return GDriveInfo(oid, fhash, path, pids=pids) 

    def exists_path(self, path) -> bool:
        if path in self._ids:
            return True
        return self.info_path(path) is not None

    def get_parent_id(self, path):
        if not path:
            return None

        parent, _ = self.split(path)

        if parent == path:
            return self._ids.get(parent)

        if not self.exists_path(parent):
            raise CloudFileNotFoundError("parent %s must exist" % parent)

        return self._ids[parent]

    @staticmethod
    def hash_data(file_like):
        md5 = hashlib.md5()
        for c in iter(lambda: file_like.read(4096), b''):
            md5.update(c)
        retval = md5.hexdigest()
        return retval

    def _path_oid(self, oid) -> str:
        "convert oid to path"
        for p, pid in self._ids.items():
            if pid == oid:
                return p

        # todo, better cache, keep up to date, etc.

        info = self._info_oid(oid)
        if info and info.pids and info.name:
            ppath = self._path_oid(info.pids[0])
            if ppath:
                path = ppath + "/" + info.name
                self._ids[path] = oid
                return path
        return None

    def info_oid(self, oid) -> ProviderInfo:
        info = self._info_oid(oid)
        # expensive
        path = self._path_oid(oid)
        ProviderInfo(info.oid, info.hash, path)

    def _info_oid(self, oid) -> GDriveInfo:
        try:
            res = self._api('files', 'get',
                    fileId=oid,
                    fields='name, md5Checksum, parents',
                    )
        except CloudFileNotFoundError:
            return None

        log.debug("info oid %s", res)

        pids = res.get('parents')
        fhash = res.get('md5Checksum')
        name = res.get('name')

        return GDriveInfo(oid, fhash, None, pids=pids, name=name)
