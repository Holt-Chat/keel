from app.router import Router
from app.compat import request
from app.responses import jsonify
from app.api_utils import make_json_error, logged_in, sliding_window_rate_limiter, timestamp, validate_request_data
from app.config import config, generate
from app.db import SQLite

push_bp=Router("push")

@push_bp.route("/me/push", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
@validate_request_data({"endpoint": {}, "p256dh": {}, "auth": {}}, source="json")
def subscribe_push(db:SQLite, id):
    if not config["push"]["enabled"]: return make_json_error(403, "Push notifications are disabled")
    endpoint=request.json.get("endpoint")
    p256dh=request.json.get("p256dh")
    auth=request.json.get("auth")
    if not all(isinstance(v, str) for v in (endpoint, p256dh, auth)): return make_json_error(400, "Invalid subscription")
    if len(endpoint)>2000 or len(p256dh)>200 or len(auth)>100: return make_json_error(400, "Invalid subscription")
    if db.exists("push_subscriptions", {"endpoint": endpoint}):
        db.update_data("push_subscriptions", {"user_id": id, "p256dh": p256dh, "auth": auth}, {"endpoint": endpoint})
    else:
        db.insert_data("push_subscriptions", {"id": generate(), "user_id": id, "endpoint": endpoint, "p256dh": p256dh, "auth": auth, "created_at": timestamp()})
    return jsonify({"success": True})

@push_bp.route("/me/push", methods=["DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=30, window=60, user_limit=15)
@validate_request_data({"endpoint": {}}, source="json")
def unsubscribe_push(db:SQLite, id):
    endpoint=request.json.get("endpoint")
    if not isinstance(endpoint, str): return make_json_error(400, "Invalid endpoint")
    db.delete_data("push_subscriptions", {"endpoint": endpoint, "user_id": id})
    return jsonify({"success": True})
