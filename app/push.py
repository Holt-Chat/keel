import os
import json
import base64
import threading
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from app.config import config, logger
from app.db import SQLite

try:
    from pywebpush import webpush, WebPushException
    _available=True
except ImportError:
    _available=False

_vapid_path=os.path.join(os.path.dirname(config["data_dir"]["database"]) or ".", "vapid.json")
vapid_private_key=None
vapid_public_key=None

def init_vapid():
    """Load or generate the VAPID keypair used to authenticate Web Push requests"""
    global vapid_private_key, vapid_public_key
    if not config["push"]["enabled"] or not _available: return
    if os.path.isfile(_vapid_path):
        with open(_vapid_path) as f: data=json.load(f)
        vapid_private_key=data["private"]
        vapid_public_key=data["public"]
        return
    priv=ec.generate_private_key(ec.SECP256R1())
    vapid_private_key=priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    point=priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    vapid_public_key=base64.urlsafe_b64encode(point).rstrip(b"=").decode()
    os.makedirs(os.path.dirname(_vapid_path) or ".", exist_ok=True)
    with open(_vapid_path, "w") as f: json.dump({"private": vapid_private_key, "public": vapid_public_key}, f)

def get_public_key():
    return vapid_public_key

def deliver_push(user_ids, payload):
    """Fan a Web Push out to the given users in the background (best-effort)"""
    if not config["push"]["enabled"] or not _available or not vapid_private_key or not user_ids: return
    threading.Thread(target=_deliver, args=(list(user_ids), payload), daemon=True).start()

def _deliver(user_ids, payload):
    with SQLite() as db:
        for user_id in user_ids:
            user=db.select_data("users", ["status"], {"id": user_id})
            if user and user[0]["status"]=="dnd": continue
            subs=db.select_data("push_subscriptions", ["id", "endpoint", "p256dh", "auth"], {"user_id": user_id})
            for sub in subs:
                try:
                    webpush(subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}, data=json.dumps(payload), vapid_private_key=vapid_private_key, vapid_claims={"sub": config["push"]["vapid_subject"]})
                except WebPushException as e:
                    if e.response is not None and e.response.status_code in (404, 410): db.delete_data("push_subscriptions", {"id": sub["id"]})
                    else: logger.warning(f"Web push failed: {e}")
                except Exception as e:
                    logger.warning(f"Web push error: {e}")
