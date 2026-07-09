import io
import json as _json
import os
from contextvars import ContextVar
from urllib.parse import parse_qsl
from starlette.requests import Request
from starlette.datastructures import UploadFile

_ctx: ContextVar["RequestProxy"]=ContextVar("request_proxy")
event_loop_var: ContextVar=ContextVar("event_loop", default=None)

class MultiDict:
    def __init__(self, items=None):
        self._items=list(items) if items else []
    def __contains__(self, key): return any(k==key for k, _ in self._items)
    def __getitem__(self, key):
        for k, v in self._items:
            if k==key: return v
        raise BadRequestKeyError(key)
    def get(self, key, default=None):
        for k, v in self._items:
            if k==key: return v
        return default
    def getlist(self, key): return [v for k, v in self._items if k==key]
    def keys(self): return [k for k, _ in self._items]
    def items(self): return list(self._items)
    def __iter__(self): return iter(k for k, _ in self._items)
    def __bool__(self): return bool(self._items)
    def __len__(self): return len(self._items)

class BadRequestKeyError(KeyError):
    pass

class FileStorage:
    """Werkzeug FileStorage-compatible wrapper backed by an in-memory stream."""
    def __init__(self, stream: io.BytesIO, filename: str, content_type: str, size: int):
        self.stream=stream
        self.filename=filename or ""
        self.content_type=content_type
        self.mimetype=(content_type or "").split(";")[0].strip() or None
        self.content_length=size
    def save(self, dst):
        self.stream.seek(0)
        with open(dst, "wb") as f:
            while True:
                chunk=self.stream.read(1024*1024)
                if not chunk: break
                f.write(chunk)
        self.stream.seek(0)
    def read(self, *args): return self.stream.read(*args)

class RequestProxy:
    def __init__(self, scope, headers, method, path, remote_addr, host_url, script_root, args, form, files, raw_body, body_size, over_limit):
        self.headers=headers
        self.method=method
        self.path=path
        self.remote_addr=remote_addr
        self.host_url=host_url
        self.script_root=script_root
        self.args=args
        self._form=form
        self._files=files
        self._raw_body=raw_body
        self._body_size=body_size
        self._over_limit=over_limit
        self._json_cached=False
        self._json_value=None

    def _check_limit(self):
        if self._over_limit:
            from app.responses import HTTPAbort
            raise HTTPAbort(413)

    @property
    def form(self):
        self._check_limit()
        return self._form

    @property
    def files(self):
        self._check_limit()
        return self._files

    @property
    def json(self):
        return self.get_json(silent=True)

    def get_json(self, force=False, silent=False):
        if self._json_cached: return self._json_value
        self._check_limit()
        ctype=self.headers.get("content-type", "")
        if not force and "application/json" not in ctype.lower():
            if silent:
                self._json_cached=True
                self._json_value=None
                return None
        try:
            value=_json.loads(self._raw_body) if self._raw_body else None
        except Exception:
            value=None
            if not silent: raise
        self._json_cached=True
        self._json_value=value
        return value

class _RequestLocal:
    def __getattr__(self, name): return getattr(_ctx.get(), name)

request=_RequestLocal()

def _build_host_url(scope, headers):
    scheme=headers.get("x-forwarded-proto") or scope.get("scheme", "http")
    host=headers.get("host", "")
    root=scope.get("root_path", "") or ""
    return f"{scheme}://{host}{root}/"

async def build_proxy(req: Request) -> RequestProxy:
    headers={k.decode().lower(): v.decode() for k, v in req.scope.get("headers", [])}
    method=req.method
    root_path=req.scope.get("root_path", "") or ""
    raw_path=req.scope.get("path", "")
    path=root_path+raw_path
    client=req.client
    remote_addr=client.host if client else None
    forwarded=headers.get("x-forwarded-for")
    if forwarded: remote_addr=forwarded.split(",")[0].strip()
    qs=req.scope.get("query_string", b"").decode()
    args=MultiDict(parse_qsl(qs, keep_blank_values=True))
    ctype=headers.get("content-type", "")
    from app.config import config as _config
    max_len=_config["server"]["max_content_length"]
    form_items=[]
    file_items=[]
    raw_body=b""
    is_form="multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype
    if method in ("GET", "HEAD", "DELETE") and not headers.get("content-length") and not is_form:
        body_size=0
    else:
        raw_body=await req.body()
        body_size=len(raw_body)
    over_limit=max_len is not None and body_size>max_len
    if is_form and not over_limit:
        async with req.form(max_files=10000, max_fields=10000) as form_data:
            for key, value in form_data.multi_items():
                if isinstance(value, UploadFile):
                    data=await value.read()
                    file_items.append((key, FileStorage(io.BytesIO(data), value.filename, value.content_type or "application/octet-stream", len(data))))
                else:
                    form_items.append((key, value))
        raw_body=b""
    host_url=_build_host_url(req.scope, headers)
    return RequestProxy(req.scope, _Headers(headers), method, path, remote_addr, host_url, root_path, args, MultiDict(form_items), MultiDict(file_items), raw_body, body_size, over_limit)

class _Headers:
    def __init__(self, d): self._d=d
    def __contains__(self, key): return key.lower() in self._d
    def __getitem__(self, key): return self._d[key.lower()]
    def get(self, key, default=None): return self._d.get(key.lower(), default)

def set_request(proxy): return _ctx.set(proxy)
def reset_request(token): _ctx.reset(token)
