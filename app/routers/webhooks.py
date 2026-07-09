from app.router import Router
from app.compat import request
from app.responses import jsonify
import json
import re
from app.api_utils import make_json_error, logged_in, sliding_window_rate_limiter, timestamp, perm, has_permission
from app.routers.stream import message_sent
from app.config import generate, config
from app.db import SQLite

webhooks_bp=Router("webhooks")
data_uri_regex=re.compile(r"^data:image\/(png|jpeg|jpg|webp|gif|x-icon);base64,[A-Za-z0-9+/=]+$")

def _get_manageable_channel(db, id, channel_id):
    member_channel_data=db.execute_raw_sql("""
        SELECT m.permissions, c.type, c.permissions as channel_permissions
        FROM members m
        JOIN channels c ON m.channel_id=c.id
        WHERE m.user_id=? AND m.channel_id=?
    """, (id, channel_id))
    if not member_channel_data: return None, make_json_error(404, "Channel not found")
    data=member_channel_data[0]
    if data["type"]!=3: return None, make_json_error(400, "Webhooks are only supported in broadcast channels")
    if not has_permission(data["permissions"], perm.manage_channel, data["channel_permissions"]): return None, make_json_error(403, "Channel management privileges required")
    return data, None

def _build_webhook_path(channel_id, webhook_id, service=None, token=None):
    base=f"{request.host_url.rstrip('/')}{request.script_root}"
    path=f"/{config['uri_prefix']}/api/v1/channel/{channel_id}/webhooks/{webhook_id}" if config["uri_prefix"] else f"/api/v1/channel/{channel_id}/webhooks/{webhook_id}"
    if service: path+=f"/{service}"
    if token: path+=f"?token={token}"
    return f"{base}{path}"

def _get_webhook_data(db, channel_id, webhook_id, token):
    webhook_data=db.execute_raw_sql("""
        SELECT w.id, w.channel_id, w.name, w.pfp, w.token, c.type
        FROM webhooks w
        JOIN channels c ON c.id=w.channel_id
        WHERE w.id=? AND w.channel_id=?
    """, (webhook_id, channel_id))
    if not webhook_data: return None, make_json_error(404, "Webhook not found")
    if webhook_data[0]["token"]!=token: return None, make_json_error(401, "Invalid webhook token")
    if webhook_data[0]["type"]!=3: return None, make_json_error(400, "Webhooks are only supported in broadcast channels")
    return webhook_data[0], None

def _truncate_webhook_content(content):
    return content[:config["messages"]["max_message_length"]]

def _validate_webhook_pfp(pfp):
    if pfp is None or pfp=="": return None, None
    pfp=pfp.strip()
    if len(pfp)>131072: return None, make_json_error(400, "Webhook pfp is too large")
    if not data_uri_regex.fullmatch(pfp): return None, make_json_error(400, "Webhook pfp must be an image data URI")
    return pfp, None

def _github_message(payload):
    event=request.headers.get("X-GitHub-Event", "github")
    repository=(payload.get("repository") or {}).get("full_name") if isinstance(payload, dict) else None
    if event=="ping": return f"GitHub webhook ping{f' from {repository}' if repository else ''}"
    if event=="push":
        ref=(payload.get("ref") or "").split("/")[-1]
        pusher=(payload.get("pusher") or {}).get("name") or (payload.get("sender") or {}).get("login") or "unknown"
        commits=len(payload.get("commits") or [])
        return _truncate_webhook_content(f"[{repository or 'GitHub'}] {pusher} pushed {commits} commit{'s' if commits!=1 else ''} to {ref or 'unknown'}")
    if event=="pull_request":
        action=payload.get("action") or "updated"
        pr=payload.get("pull_request") or {}
        title=pr.get("title") or "pull request"
        return _truncate_webhook_content(f"[{repository or 'GitHub'}] pull request {action}: {title}")
    if event=="issues":
        action=payload.get("action") or "updated"
        issue=payload.get("issue") or {}
        title=issue.get("title") or "issue"
        return _truncate_webhook_content(f"[{repository or 'GitHub'}] issue {action}: {title}")
    if event=="issue_comment":
        action=payload.get("action") or "created"
        issue=payload.get("issue") or {}
        title=issue.get("title") or "issue"
        return _truncate_webhook_content(f"[{repository or 'GitHub'}] comment {action} on: {title}")
    if event=="release":
        action=payload.get("action") or "published"
        release=payload.get("release") or {}
        name=release.get("name") or release.get("tag_name") or "release"
        return _truncate_webhook_content(f"[{repository or 'GitHub'}] release {action}: {name}")
    return _truncate_webhook_content(f"GitHub event received: {event}{f' in {repository}' if repository else ''}")

def _get_webhook_content(service):
    payload=request.get_json(force=True, silent=True)
    if service is not None: service=service.lower()
    if service not in [None, "discord", "github"]: return None, make_json_error(400, "Unsupported webhook compatibility mode")
    if not isinstance(payload, dict): return None, make_json_error(400, "Webhook payload must be JSON")
    if service=="github":
        return {"content": _github_message(payload), "name": None, "pfp": None}, None
    content=(payload.get("content") or "").strip()
    if not content and service=="discord" and payload.get("embeds"):
        embeds=[e for e in payload["embeds"] if isinstance(e, dict)]
        content='\n'.join(f"! embed: {json.dumps(e, ensure_ascii=True)}" for e in embeds[:10])
    if not content and payload: content=json.dumps(payload, ensure_ascii=True)
    if not content: return None, make_json_error(400, "content is required")
    webhook_name=(payload.get("username") or payload.get("name") or "").strip()
    webhook_pfp=payload.get("avatar_url") if service=="discord" else payload.get("pfp")
    webhook_pfp, error_resp=_validate_webhook_pfp(webhook_pfp)
    if error_resp: return None, error_resp
    if webhook_name and len(webhook_name)>50: return None, make_json_error(400, "Invalid webhook username parameter, error: length")
    return {"content": _truncate_webhook_content(content), "name": webhook_name or None, "pfp": webhook_pfp}, None

@webhooks_bp.route("/channel/<string:channel_id>/webhooks", methods=["GET"])
@logged_in()
@sliding_window_rate_limiter(limit=100, window=60, user_limit=50)
def list_webhooks(db:SQLite, id, channel_id):
    if not config["webhooks"]["enabled"]: return make_json_error(403, "Webhooks are disabled")
    _, error_resp=_get_manageable_channel(db, id, channel_id)
    if error_resp: return error_resp
    webhooks=db.execute_raw_sql("""
        SELECT w.id, w.name, w.pfp, w.token, w.created_at, w.last_used_at, u.username as created_by_username, u.display_name as created_by_display
        FROM webhooks w
        LEFT JOIN users u ON u.id=w.created_by
        WHERE w.channel_id=?
        ORDER BY w.created_at DESC
    """, (channel_id,))
    for webhook in webhooks:
        webhook["url"]=_build_webhook_path(channel_id, webhook["id"])
    return jsonify(webhooks)

@webhooks_bp.route("/channel/<string:channel_id>/webhooks", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=20, window=300, user_limit=10)
def create_webhook(db:SQLite, id, channel_id):
    if not config["webhooks"]["enabled"]: return make_json_error(403, "Webhooks are disabled")
    _, error_resp=_get_manageable_channel(db, id, channel_id)
    if error_resp: return error_resp
    data=request.get_json(force=True, silent=True)
    if not isinstance(data, dict): return make_json_error(400, "Webhook payload must be JSON")
    name=(data.get("name") or "").strip()
    if len(name)<1 or len(name)>50: return make_json_error(400, "Invalid name parameter, error: length")
    pfp, error_resp=_validate_webhook_pfp(data.get("pfp"))
    if error_resp: return error_resp
    webhook_id=generate()
    webhook_token=generate(32)
    now=timestamp(True)
    db.insert_data("webhooks", {"id": webhook_id, "channel_id": channel_id, "name": name, "pfp": pfp, "token": webhook_token, "created_by": id, "created_at": now, "last_used_at": None})
    webhook={"id": webhook_id, "name": name, "pfp": pfp, "created_at": now, "last_used_at": None, "url": _build_webhook_path(channel_id, webhook_id)}
    return jsonify({"webhook": webhook, "token": webhook_token, "send_url": _build_webhook_path(channel_id, webhook_id, token=webhook_token), "success": True}), 201

@webhooks_bp.route("/channel/<string:channel_id>/webhooks/<string:webhook_id>", methods=["POST", "DELETE"])
@webhooks_bp.route("/channel/<string:channel_id>/webhooks/<string:webhook_id>/<string:service>", methods=["POST"])
@sliding_window_rate_limiter(limit=100, window=60)
def webhook_action(channel_id, webhook_id, service=None):
    if not config["webhooks"]["enabled"]: return make_json_error(403, "Webhooks are disabled")
    db=SQLite()
    try:
        token=request.args.get("token")
        if not token: return make_json_error(401, "Webhook token is required")
        webhook_data, error_resp=_get_webhook_data(db, channel_id, webhook_id, token)
        if error_resp: return error_resp
        if request.method=="DELETE":
            db.delete_data("webhooks", {"id": webhook_id, "channel_id": channel_id})
            return jsonify({"success": True})
        payload, error_resp=_get_webhook_content(service)
        if error_resp: return error_resp
        sent_at=timestamp(True)
        message_id=generate()
        webhook_name=payload["name"] or webhook_data["name"]
        webhook_pfp=payload["pfp"] or webhook_data["pfp"]
        db.insert_data("messages", {"id": message_id, "channel_id": channel_id, "user_id": "0", "content": payload["content"], "key": None, "iv": None, "timestamp": sent_at, "replied_to": None, "signature": None, "signed_timestamp": None, "nonce": None, "webhook_id": webhook_id, "webhook_name": webhook_name, "webhook_pfp": webhook_pfp})
        db.update_data("webhooks", {"last_used_at": sent_at}, {"id": webhook_id})
        message_data={"id": message_id, "content": payload["content"], "key": None, "iv": None, "timestamp": sent_at, "edited_at": None, "replied_to": None, "user": {"username": None, "display": webhook_name, "pfp": webhook_pfp}, "attachments": [], "signature": None, "signed_timestamp": None, "nonce": None, "webhook_id": webhook_id}
        message_sent(channel_id, message_data, "0", db)
        return jsonify({"message_id": message_id, "success": True}), 201
    finally:
        db.close()
