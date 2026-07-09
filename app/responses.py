import json as _json
import os
import html as _html
from starlette.responses import Response, FileResponse, PlainTextResponse

class FlaskJSONResponse(Response):
    media_type="application/json"
    def render(self, content) -> bytes:
        return (_json.dumps(content, ensure_ascii=True, separators=(",", ":"), sort_keys=True)+"\n").encode("utf-8")

class HTTPAbort(Exception):
    def __init__(self, code): self.code=code

def abort(code): raise HTTPAbort(code)

class FlaskResponse:
    """Mutable response holder mirroring the parts of a Flask Response the handlers use."""
    def __init__(self, body=None, status=200, mimetype=None, json_body=None, is_json=False, file_path=None, file_kwargs=None, redirect_to=None, redirect_code=302):
        self.body=body
        self.status_code=status
        self.mimetype=mimetype
        self.json_body=json_body
        self.is_json=is_json
        self.file_path=file_path
        self.file_kwargs=file_kwargs or {}
        self.redirect_to=redirect_to
        self.redirect_code=redirect_code
        self.headers={}

def jsonify(*args, **kwargs):
    if args and kwargs: raise TypeError("jsonify behavior undefined for both args and kwargs")
    if len(args)==1: data=args[0]
    elif args: data=list(args)
    else: data=kwargs
    return FlaskResponse(json_body=data, is_json=True, mimetype="application/json")

def make_response(rv):
    if isinstance(rv, (FlaskResponse, Response)): return rv
    if isinstance(rv, tuple):
        resp=make_response(rv[0])
        if len(rv)>=2 and rv[1] is not None: resp.status_code=rv[1]
        if len(rv)>=3 and rv[2]: resp.headers.update(rv[2])
        return resp
    if isinstance(rv, (dict, list)): return FlaskResponse(json_body=rv, is_json=True, mimetype="application/json")
    if isinstance(rv, str): return FlaskResponse(body=rv, mimetype="text/html")
    return FlaskResponse(body=rv)

def redirect(location, code=302): return FlaskResponse(redirect_to=location, redirect_code=code)

def send_from_directory(directory, filename, mimetype=None, as_attachment=False, download_name=None):
    path=os.path.join(directory, filename)
    if not os.path.isfile(path): raise HTTPAbort(404)
    return FlaskResponse(file_path=path, file_kwargs={"mimetype": mimetype, "as_attachment": as_attachment, "download_name": download_name})

def to_starlette(rv) -> Response:
    resp=make_response(rv)
    if isinstance(resp, Response): return resp
    if resp.redirect_to is not None:
        loc=resp.redirect_to
        display=_html.escape(loc)
        body=(f"<!doctype html>\n<html lang=en>\n<title>Redirecting...</title>\n"
              f"<h1>Redirecting...</h1>\n<p>You should be redirected automatically to the target URL: "
              f"<a href=\"{display}\">{display}</a>. If not, click the link.\n")
        out=Response(content=body, status_code=resp.redirect_code, media_type="text/html; charset=utf-8")
        out.headers["Location"]=loc
    elif resp.file_path is not None:
        fk=resp.file_kwargs
        disposition="attachment" if fk.get("as_attachment") else "inline"
        out=FileResponse(resp.file_path, media_type=fk.get("mimetype"), filename=fk.get("download_name") if fk.get("as_attachment") else None, content_disposition_type=disposition)
    elif resp.is_json:
        out=FlaskJSONResponse(content=resp.json_body, status_code=resp.status_code)
    elif resp.mimetype and resp.mimetype.startswith("text/") and not isinstance(resp.body, (bytes, bytearray)):
        out=PlainTextResponse(resp.body if resp.body is not None else "", status_code=resp.status_code, media_type=resp.mimetype)
    else:
        body=resp.body if resp.body is not None else b""
        if isinstance(body, str): body=body.encode()
        out=Response(content=body, status_code=resp.status_code, media_type=resp.mimetype)
    if resp.redirect_to is None and resp.file_path is None:
        out.status_code=resp.status_code
    for k, v in resp.headers.items():
        out.headers[k]=v
    return out
