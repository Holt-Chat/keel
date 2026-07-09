from app.router import Router
from app.compat import request
from app.responses import jsonify
import json
from app.api_utils import (
    make_json_error, logged_in, sliding_window_rate_limiter,
    timestamp, perm, has_permission, validate_request_data,
    get_file_size_chunked, ephemeral_ttls
)
from app.config import generate
from app.routers.stream import message_sent, message_edited, message_deleted, dm_unhide, typing_signal, presence_pair_sync
from app.config import config
from app.db import SQLite
import os
import math

max_encrypted_msg_len=math.ceil((config["messages"]["max_message_length"]+16)/3)*4

messages_bp=Router("messages")

os.makedirs(config["data_dir"]["attachments"], exist_ok=True)

@messages_bp.route("/channel/<string:channel_id>/messages")
@logged_in()
@sliding_window_rate_limiter(limit=200, window=60, user_limit=100)
def channel_messages(db:SQLite, id, channel_id):
    member_channel_data=db.execute_raw_sql("""
        SELECT m.permissions, m.message_seq, c.type, c.permissions as channel_permissions
        FROM members m
        JOIN channels c ON m.channel_id=c.id
        WHERE m.user_id=? AND m.channel_id=?
    """, (id, channel_id))
    if not member_channel_data: return make_json_error(404, "Channel not found")
    data=member_channel_data[0]
    user_permissions=data["permissions"]
    member_message_seq=data["message_seq"]
    channel_permissions=data["channel_permissions"]
    hide_author=(
        data["type"]==3 and not (
            has_permission(user_permissions, perm.send_messages, channel_permissions)
            or has_permission(user_permissions, perm.manage_members, channel_permissions)
            or has_permission(user_permissions, perm.manage_permissions, channel_permissions)
        )
    )
    limit=int(request.args.get("limit", 50))
    offset=int(request.args.get("offset", 0))
    before_messages=int(request.args.get("before_messages", 0))
    if limit>100: limit=100
    if limit<1: limit=1
    if before_messages<0: before_messages=0
    if before_messages>100: before_messages=100
    if hide_author:
        sql_parts=[
            "SELECT m.content, m.id, m.key, m.iv, m.timestamp, m.edited_at, m.replied_to, m.nonce, m.webhook_id, m.expires_at, ",
            "NULL AS user, ",
            "NULL AS signature, ",
            "NULL AS signed_timestamp, ",
            "(SELECT json_group_array(json_object(",
            "   'id', am.file_id, ",
            "   'filename', f.filename, ",
            "   'size', f.size, ",
            "   'mimetype', f.mimetype, ",
            "   'encrypted', am.encrypted, ",
            "   'iv', am.iv",
            ")) FROM attachment_message am ",
            "   JOIN files f ON am.file_id = f.id ",
            "   WHERE am.message_id = m.id) AS attachments, m.components ",
            "FROM messages m ",
            "WHERE m.channel_id = ? AND m.seq > ? AND (m.expires_at IS NULL OR m.expires_at > ?)"
        ]
    else:
        sql_parts=[
            "SELECT m.content, m.id, m.key, m.iv, m.timestamp, m.edited_at, m.replied_to, m.nonce, m.webhook_id, m.expires_at, ",
            "json_object(",
            "  'username', CASE WHEN m.user_id='0' THEN NULL ELSE u.username END, ",
            "  'display', CASE WHEN m.user_id='0' THEN m.webhook_name ELSE u.display_name END, ",
            "  'pfp', CASE WHEN m.user_id='0' THEN m.webhook_pfp ELSE u.pfp END,",
            "  'is_bot', CASE WHEN m.user_id='0' THEN 0 ELSE u.is_bot END",
            ") AS user, ",
            "m.signature, ",
            "m.signed_timestamp, ",
            "(SELECT json_group_array(json_object(",
            "   'id', am.file_id, ",
            "   'filename', f.filename, ",
            "   'size', f.size, ",
            "   'mimetype', f.mimetype, ",
            "   'encrypted', am.encrypted, ",
            "   'iv', am.iv",
            ")) FROM attachment_message am ",
            "   JOIN files f ON am.file_id = f.id ",
            "   WHERE am.message_id = m.id) AS attachments, m.components ",
            "FROM messages m ",
            "JOIN users u ON m.user_id = u.id ",
            "WHERE m.channel_id = ? AND m.seq > ? AND (m.expires_at IS NULL OR m.expires_at > ?)"
        ]
    params=[channel_id, member_message_seq, timestamp(True)]
    if "user_id" in request.args:
        if request.args["user_id"]!="0" and len(request.args["user_id"])!=20: return make_json_error(400, "Invalid user_id parameter, error: length")
        sql_parts.append("AND m.user_id=?")
        params.append(request.args["user_id"])
    if "before" in request.args and "after" in request.args:
        sql_parts.append("AND m.timestamp BETWEEN ? AND ?")
        params.extend([int(request.args["after"]), int(request.args["before"])])
    elif "before" in request.args:
        sql_parts.append("AND m.timestamp < ?")
        params.append(int(request.args["before"]))
    elif "after" in request.args:
        sql_parts.append("AND m.timestamp > ?")
        params.append(int(request.args["after"]))
    if "before_message_id" in request.args and "after_message_id" in request.args:
        sql_parts.append("AND m.seq BETWEEN (SELECT seq FROM messages WHERE id=? AND channel_id=?) AND (SELECT seq FROM messages WHERE id=? AND channel_id=?)")
        params.extend([request.args["after_message_id"], channel_id, request.args["before_message_id"], channel_id])
    elif "before_message_id" in request.args:
        sql_parts.append("AND m.seq < (SELECT seq FROM messages WHERE id=? AND channel_id=?)")
        params.extend([request.args["before_message_id"], channel_id])
    elif "after_message_id" in request.args:
        sql_parts.append("AND m.seq > (SELECT seq FROM messages WHERE id=? AND channel_id=?)")
        params.extend([request.args["after_message_id"], channel_id])
    sql_parts.append("ORDER BY m.seq DESC LIMIT ? OFFSET ?")
    total_limit=limit+before_messages
    params.extend([total_limit, offset])
    messages=db.execute_raw_sql(" ".join(sql_parts), params)
    for msg in messages:
        msg["user"]=json.loads(msg["user"]) if msg["user"] else None
        msg["attachments"]=[{**a, "encrypted": bool(a["encrypted"])} for a in json.loads(msg["attachments"])]
        msg["components"]=json.loads(msg["components"]) if msg["components"] else None
    return jsonify(messages)

@messages_bp.route("/channel/<string:channel_id>/embed-asset", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=100, window=60, user_limit=50)
def upload_embed_asset(db:SQLite, id, channel_id):
    if "image" not in request.files or not request.files["image"].filename: return make_json_error(400, "image file is required")
    image=request.files["image"]
    member_channel_data=db.execute_raw_sql("""
        SELECT m.permissions, c.type, c.permissions as channel_permissions
        FROM members m
        JOIN channels c ON m.channel_id=c.id
        WHERE m.user_id=? AND m.channel_id=?
    """, (id, channel_id))
    if not member_channel_data: return make_json_error(404, "Channel not found")
    data=member_channel_data[0]
    if not has_permission(data["permissions"], perm.send_messages, data["channel_permissions"]): return make_json_error(403, "No permission to send messages")
    if not (image.mimetype or "").startswith("image/"): return make_json_error(400, "File must be an image")
    if (image.content_length or get_file_size_chunked(image, config["max_file_size"]["attachments"]))>config["max_file_size"]["attachments"]: return make_json_error(413, "Image exceeds file size limit")
    encrypted=data["type"]!=3 and request.form.get("encrypted")=="1"
    key=None
    iv=None
    if encrypted:
        if "key" not in request.form or "iv" not in request.form: return make_json_error(400, "key and iv is required for encrypted uploads")
        key=request.form["key"]
        key_info=db.execute_raw_sql(
            "SELECT expires_at FROM channels_keys_info WHERE channel_id=? AND key_id=?",
            (channel_id, key)
        )
        if not key_info: return make_json_error(400, "Invalid or outdated encryption key")
        if key_info[0]["expires_at"]<timestamp(True): return make_json_error(403, "No encryption key available")
        if len(request.form["iv"])!=16: return make_json_error(400, "Invalid iv parameter, error: length")
        iv=request.form["iv"]
    temp_path=os.path.join(config["data_dir"]["attachments"], f"temp_{generate()}")
    image.save(temp_path)
    file_hash=db.calculate_file_hash(temp_path)
    file_size=os.path.getsize(temp_path)
    existing_file=db.select_data("files", ["id"], {"hash": file_hash, "file_type": "attachment"})
    if existing_file:
        os.remove(temp_path)
        file_id=existing_file[0]["id"]
    else:
        file_id=generate()
        final_path=os.path.join(config["data_dir"]["attachments"], file_id)
        os.rename(temp_path, final_path)
        db.insert_data("files", {"id": file_id, "filename": image.filename, "hash": file_hash, "size": file_size, "mimetype": image.content_type, "file_type": "attachment"})
    db.insert_data("embed_assets", {"id": generate(), "file_id": file_id, "channel_id": channel_id, "uploader_id": id, "key_id": key, "iv": iv, "encrypted": 1 if encrypted else 0, "created_at": timestamp(True)})
    return jsonify({"id": file_id, "encrypted": encrypted, "iv": iv, "success": True}), 201

@messages_bp.route("/channel/<string:channel_id>/messages", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=100, window=60, user_limit=50)
@validate_request_data({"content": {}, "timestamp": {}, "signature": {}})
def sending_messages(db:SQLite, id, channel_id):
    files=request.files.getlist("files")
    msg=request.form["content"].replace("\r\n", "\n").replace("\r", "\n").strip()
    has_files=any(file.filename for file in files)
    if (not has_files and not msg): return make_json_error(400, "content or files required")
    replied_to=request.form.get("replied_to")
    try: signed_timestamp=int(request.form["timestamp"])
    except ValueError: return make_json_error(400, "Invalid timestamp format")
    signature=request.form["signature"]
    current_time=timestamp()
    if abs(current_time-signed_timestamp)>config["messages"]["signature_timestamp_window"]: return make_json_error(400, "Timestamp is invalid")
    if replied_to and not db.exists("messages", {"id": replied_to, "channel_id": channel_id}): return make_json_error(400, "replied_to message not found in this channel")
    ttl=request.form.get("ttl")
    if ttl and ttl not in ephemeral_ttls: return make_json_error(400, "Invalid ttl parameter")
    member_channel_data=db.execute_raw_sql("""
        SELECT m.permissions, c.type, c.permissions as channel_permissions, c.default_ttl
        FROM members m
        JOIN channels c ON m.channel_id=c.id
        WHERE m.user_id=? AND m.channel_id=?
    """, (id, channel_id))
    if not member_channel_data: return make_json_error(404, "Channel not found")
    data=member_channel_data[0]
    if data["type"]==1:
        other_member=db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=? AND user_id!=?", (channel_id, id))
        if other_member and db.exists("blocks", {"blocker_id": other_member[0]["user_id"], "blocked_id": id}): return make_json_error(403, "You are blocked by this user")
    member_permissions=data["permissions"]
    channel_permissions=data["channel_permissions"]
    if not has_permission(member_permissions, perm.send_messages, channel_permissions): return make_json_error(403, "No permission to send messages")
    if len(msg)>(config["messages"]["max_message_length"] if data["type"]==3 else max_encrypted_msg_len): return make_json_error(400, "Message too long")
    key=None
    iv=None
    if data["type"]!=3:
        if "key" not in request.form or "iv" not in request.form: return make_json_error(400, "key and iv is required in non-broadcast channels")
        key=request.form["key"]
        key_info=db.execute_raw_sql(
            "SELECT expires_at FROM channels_keys_info WHERE channel_id=? AND key_id=?",
            (channel_id, key)
        )
        if not key_info: return make_json_error(400, "Invalid or outdated encryption key")
        if key_info[0]["expires_at"]<timestamp(True): return make_json_error(403, "No encryption key available")
        if len(request.form["iv"])!=16: return make_json_error(400, "Invalid iv parameter, error: length")
        iv=request.form["iv"]
    nonce=request.form.get("nonce")
    components_raw=request.form.get("components")
    components_str=None
    if components_raw:
        if not db.exists("users", {"id": id, "is_bot": 1}): return make_json_error(403, "components are bot-only")
        try: components_data=json.loads(components_raw)
        except: return make_json_error(400, "Invalid components format")
        err=_validate_components(components_data)
        if err: return make_json_error(400, err)
        components_str=components_raw
    attachments_meta_raw=request.form.getlist("attachments_meta")
    attachments_meta=[]
    for item in attachments_meta_raw:
        try: attachments_meta.append(json.loads(item))
        except: return make_json_error(400, "Invalid attachments_meta format")
    for idx, file in enumerate(files):
        if file.filename:
            meta=attachments_meta[idx] if idx<len(attachments_meta) else {}
            encrypted=meta.get("encrypted", False)
            attachment_iv=meta.get("iv")
            if encrypted and not attachment_iv: return make_json_error(400, "iv required when encrypted=true")
            if encrypted and len(attachment_iv)!=16: return make_json_error(400, "Invalid iv length for attachment")
    embed_asset_ids_raw=request.form.get("embed_asset_ids")
    embed_asset_ids=[]
    if embed_asset_ids_raw:
        try: embed_asset_ids=json.loads(embed_asset_ids_raw)
        except: return make_json_error(400, "Invalid embed_asset_ids format")
        if not isinstance(embed_asset_ids, list) or not all(isinstance(a, str) for a in embed_asset_ids): return make_json_error(400, "Invalid embed_asset_ids format")
        if len(embed_asset_ids)>40: return make_json_error(400, "Too many embed asset references")
        for asset_id in embed_asset_ids:
            if not db.exists("embed_assets", {"file_id": asset_id, "channel_id": channel_id, "uploader_id": id, "message_id": None}): return make_json_error(400, "Invalid embed asset reference")
    message_id=generate()
    sent_at=timestamp(True)
    expires_at=sent_at+ephemeral_ttls[ttl] if ttl else (sent_at+data["default_ttl"] if data["default_ttl"] else None)
    db.insert_data("messages", {"id": message_id, "channel_id": channel_id, "user_id": id, "content": msg, "key": key, "iv": iv, "timestamp": sent_at, "replied_to": replied_to, "signature": signature, "signed_timestamp": signed_timestamp, "nonce": nonce, "components": components_str, "expires_at": expires_at})
    for asset_id in embed_asset_ids:
        db.update_data("embed_assets", {"message_id": message_id}, {"file_id": asset_id, "channel_id": channel_id, "uploader_id": id, "message_id": None})
    if config["presence"]["enabled"]: db.execute_raw_sql("UPDATE users SET last_seen=? WHERE id=? AND status!='invisible'", (sent_at, id))
    if db.exists("message_reads", {"user_id": id, "channel_id": channel_id}): db.update_data("message_reads", {"last_message_id": message_id, "read_at": sent_at}, {"user_id": id, "channel_id": channel_id})
    else: db.insert_data("message_reads", {"user_id": id, "channel_id": channel_id, "last_message_id": message_id, "read_at": sent_at})
    attachments=[]
    for idx, file in enumerate(files):
        if file.filename and (file.content_length is None or file.content_length <= config["max_file_size"]["attachments"]) and get_file_size_chunked(file, config["max_file_size"]["attachments"])<=config["max_file_size"]["attachments"]:
            meta=attachments_meta[idx] if idx<len(attachments_meta) else {}
            encrypted=meta.get("encrypted", False)
            attachment_iv=meta.get("iv")
            temp_path=os.path.join(config["data_dir"]["attachments"], f"temp_{generate()}")
            file.save(temp_path)
            file_hash=db.calculate_file_hash(temp_path)
            file_size=os.path.getsize(temp_path)
            existing_file=db.select_data("files", ["id", "filename", "size", "mimetype"], {"hash": file_hash, "file_type": "attachment"})
            if existing_file:
                os.remove(temp_path)
                file_id=existing_file[0]["id"]
                file_info=existing_file[0]
            else:
                file_id=generate()
                final_path=os.path.join(config["data_dir"]["attachments"], file_id)
                os.rename(temp_path, final_path)
                db.insert_data("files", {"id": file_id, "filename": file.filename, "hash": file_hash, "size": file_size, "mimetype": file.content_type, "file_type": "attachment"})
                file_info={"id": file_id, "filename": file.filename, "size": file_size, "mimetype": file.content_type}
            existing_attachment=db.select_data("attachment_message", ["file_id"], {"file_id": file_id, "message_id": message_id})
            if not existing_attachment:
                db.insert_data("attachment_message", {"file_id": file_id, "message_id": message_id, "encrypted": 1 if encrypted else 0, "iv": attachment_iv})
            attachments.append({"id": file_id, "filename": file.filename, "size": file_info["size"], "mimetype": file_info["mimetype"], "encrypted": bool(encrypted), "iv": attachment_iv})
    if not msg and has_files and not attachments:
        db.delete_data("messages", {"id": message_id})
        return make_json_error(400, "Files do not meet size requirements")
    # Get user data for the emit
    user_data=db.execute_raw_sql("SELECT username, display_name AS display, pfp, is_bot FROM users WHERE id=?", (id,))[0] if not (data["type"]==3 and not (has_permission(member_permissions, perm.send_messages, channel_permissions) or has_permission(member_permissions, perm.manage_members, channel_permissions) or has_permission(member_permissions, perm.manage_permissions, channel_permissions))) else None
    hide_signature=(data["type"]==3 and not (has_permission(member_permissions, perm.send_messages, channel_permissions) or has_permission(member_permissions, perm.manage_members, channel_permissions) or has_permission(member_permissions, perm.manage_permissions, channel_permissions)))
    message_data={
        "id": message_id,
        "content": msg,
        "key": key,
        "iv": iv,
        "timestamp": sent_at,
        "edited_at": None,
        "replied_to": replied_to,
        "user": user_data,
        "attachments": attachments,
        "signature": None if hide_signature else signature,
        "signed_timestamp": None if hide_signature else signed_timestamp,
        "nonce": nonce,
        "components": json.loads(components_str) if components_str else None,
        "expires_at": expires_at
    }
    if data["type"]==1:
        current_member=db.select_data("members", ["hidden"], {"channel_id": channel_id, "user_id": id})
        if current_member and current_member[0]["hidden"]:
            db.update_data("members", {"hidden": None}, {"user_id": id, "channel_id": channel_id})
            dm_unhide(channel_id, id, db)
        other_member=db.execute_raw_sql("SELECT user_id, hidden FROM members WHERE channel_id=? AND user_id!=?", (channel_id, id))
        if other_member and other_member[0]["hidden"]:
            other_user_id=other_member[0]["user_id"]
            db.update_data("members", {"hidden": None}, {"user_id": other_user_id, "channel_id": channel_id})
            dm_unhide(channel_id, other_user_id, db)
        if other_member and len(db.execute_raw_sql("SELECT 1 FROM messages WHERE channel_id=? AND user_id=? LIMIT 2", (channel_id, id)))==1: presence_pair_sync(id, other_member[0]["user_id"], db)

    message_sent(channel_id, message_data, id, db)
    db.cleanup_stale_embed_assets()

    return jsonify({"message_id": message_id, "attachments": attachments, "success": True}), 201

@messages_bp.route("/channel/<string:channel_id>/message/<string:message_id>", methods=["PATCH", "DELETE"])
@logged_in()
@sliding_window_rate_limiter(limit=150, window=60, user_limit=75)
def message_management(db:SQLite, id, channel_id, message_id):
    message_channel_data=db.execute_raw_sql("""
        SELECT m.user_id, m.channel_id, m.content, m.iv, c.type, c.permissions as channel_permissions
        FROM messages m
        JOIN channels c ON m.channel_id=c.id
        WHERE m.id=?
    """, (message_id,))
    if not message_channel_data: return make_json_error(404, "Message not found")
    data=message_channel_data[0]
    if data["channel_id"]!=channel_id: return make_json_error(404, "Message not found")
    if request.method=="PATCH":
        content=request.form.get("content")
        components_raw=request.form.get("components")
        if content is None and "components" not in request.form: return make_json_error(400, "content or components required")
        if request.form.get("timestamp") is None: return make_json_error(400, "timestamp is required")
        if request.form.get("signature") is None: return make_json_error(400, "signature is required")
        if data["user_id"]!=id: return make_json_error(403, "Can only edit your own messages")
        try: signed_timestamp=int(request.form["timestamp"])
        except ValueError: return make_json_error(400, "Invalid timestamp format")
        signature=request.form["signature"]
        if abs(timestamp()-signed_timestamp)>config["messages"]["signature_timestamp_window"]: return make_json_error(400, "Timestamp is invalid")
        update_fields={"edited_at": timestamp(True), "signature": signature, "signed_timestamp": signed_timestamp}
        if content is not None:
            content=content.replace("\r\n", "\n").replace("\r", "\n")
            if len(content)>(config["messages"]["max_message_length"] if data["type"]==3 else max_encrypted_msg_len): return make_json_error(400, "Message too long")
            update_fields["content"]=content
            if data["type"]!=3:
                if "iv" not in request.form: return make_json_error(400, "iv is required in non-broadcast channels")
                if len(request.form["iv"])!=16: return make_json_error(400, "Invalid iv parameter, error: length")
                update_fields["iv"]=request.form["iv"]
        if "components" in request.form:
            if not db.exists("users", {"id": id, "is_bot": 1}): return make_json_error(403, "components are bot-only")
            if components_raw:
                try: components_data=json.loads(components_raw)
                except: return make_json_error(400, "Invalid components format")
                err=_validate_components(components_data)
                if err: return make_json_error(400, err)
                update_fields["components"]=components_raw
            else:
                update_fields["components"]=None
        embed_asset_ids_raw=request.form.get("embed_asset_ids")
        embed_asset_ids=[]
        if embed_asset_ids_raw:
            try: embed_asset_ids=json.loads(embed_asset_ids_raw)
            except: return make_json_error(400, "Invalid embed_asset_ids format")
            if not isinstance(embed_asset_ids, list) or not all(isinstance(a, str) for a in embed_asset_ids): return make_json_error(400, "Invalid embed_asset_ids format")
            if len(embed_asset_ids)>40: return make_json_error(400, "Too many embed asset references")
            for asset_id in embed_asset_ids:
                if not db.exists("embed_assets", {"file_id": asset_id, "channel_id": channel_id, "uploader_id": id, "message_id": None}): return make_json_error(400, "Invalid embed asset reference")
        db.update_data("messages", update_fields, {"id": message_id})
        for asset_id in embed_asset_ids:
            db.update_data("embed_assets", {"message_id": message_id}, {"file_id": asset_id, "channel_id": channel_id, "uploader_id": id, "message_id": None})
        updated_message=db.execute_raw_sql("""
            SELECT m.id, m.content, m.key, m.iv, m.timestamp, m.edited_at, m.replied_to, m.signature, m.signed_timestamp, m.nonce, m.webhook_id, m.components,
            json_object('username', CASE WHEN m.user_id='0' THEN NULL ELSE u.username END, 'display', CASE WHEN m.user_id='0' THEN m.webhook_name ELSE u.display_name END, 'pfp', CASE WHEN m.user_id='0' THEN m.webhook_pfp ELSE u.pfp END, 'is_bot', CASE WHEN m.user_id='0' THEN 0 ELSE u.is_bot END) as user,
            (SELECT json_group_array(json_object('id', am.file_id, 'filename', f.filename, 'size', f.size, 'mimetype', f.mimetype, 'encrypted', am.encrypted, 'iv', am.iv))
             FROM attachment_message am JOIN files f ON am.file_id = f.id WHERE am.message_id = m.id) as attachments
            FROM messages m JOIN users u ON m.user_id = u.id WHERE m.id=?
        """, (message_id,))[0]
        updated_message["user"]=json.loads(updated_message["user"])
        updated_message["attachments"]=[{**a, "encrypted": bool(a["encrypted"])} for a in json.loads(updated_message["attachments"])] if updated_message["attachments"] else []
        if updated_message["components"]: updated_message["components"]=json.loads(updated_message["components"])
        message_edited(channel_id, updated_message, id, db)
        if embed_asset_ids: db.cleanup_stale_embed_assets()
        return jsonify({"success": True})
    elif request.method=="DELETE":
        if data["user_id"]!=id:
            member_perms=db.execute_raw_sql("""
                SELECT m.permissions
                FROM members m
                WHERE m.user_id=? AND m.channel_id=?
            """, (id, channel_id))
            if not member_perms: return make_json_error(404, "Channel not found")
            channel_permissions=data["channel_permissions"]
            if not has_permission(member_perms[0]["permissions"], perm.manage_messages, channel_permissions): return make_json_error(403, "Can only delete your own messages or need manage messages permission")
        db.delete_data("messages", {"id": message_id})
        db.cleanup_unused_files()
        db.cleanup_unused_keys()

        # Emit message deleted event
        message_deleted(channel_id, message_id, id)

        return jsonify({"success": True})

@messages_bp.route("/channel/<string:channel_id>/messages/ack", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=60, window=60, user_limit=30)
def ack_message(db:SQLite, id, channel_id):
    if not db.exists("members", {"user_id": id, "channel_id": channel_id}): return make_json_error(404, "Channel not found")
    latest_message=db.execute_raw_sql(
        "SELECT id FROM messages WHERE channel_id=? ORDER BY seq DESC LIMIT 1",
        (channel_id,)
    )
    if not latest_message: return make_json_error(404, "No messages in channel")
    latest_message_id=latest_message[0]["id"]
    if db.exists("message_reads", {"user_id": id, "channel_id": channel_id}):
        db.update_data("message_reads", {"last_message_id": latest_message_id, "read_at": timestamp(True)}, {"user_id": id, "channel_id": channel_id})
    else: db.insert_data("message_reads", {"user_id": id, "channel_id": channel_id, "last_message_id": latest_message_id, "read_at": timestamp(True)})
    return jsonify({"success": True})

def _validate_components(components):
    if not isinstance(components, list): return "components must be an array"
    if len(components)>5: return "maximum 5 action rows"
    for row in components:
        if not isinstance(row, dict): return "each action row must be an object"
        if row.get("type")!=1: return "action row type must be 1"
        btns=row.get("components", [])
        if not isinstance(btns, list): return "action row components must be an array"
        if len(btns)>5: return "maximum 5 buttons per row"
        for btn in btns:
            if not isinstance(btn, dict): return "each component must be an object"
            if btn.get("type")!=2: return "only button components (type 2) are supported"
            style=btn.get("style")
            if style not in (1, 2, 3, 4, 5): return "button style must be 1-5"
            if not btn.get("label"): return "button must have a label"
            if style==5 and not btn.get("url"): return "link button (style 5) must have url"
            if style!=5 and not btn.get("custom_id"): return "non-link button must have custom_id"
    return None
@messages_bp.route("/channel/<string:channel_id>/typing", methods=["POST"])
@logged_in()
@sliding_window_rate_limiter(limit=120, window=60, user_limit=60)
def typing(db:SQLite, id, channel_id):
    if not config["typing"]["enabled"]: return make_json_error(403, "Typing indicators are disabled")
    channel=db.execute_raw_sql("SELECT c.type FROM channels c JOIN members m ON c.id=m.channel_id WHERE m.user_id=? AND m.channel_id=?", (id, channel_id))
    if not channel: return make_json_error(404, "Channel not found")
    if channel[0]["type"] not in (1, 2): return make_json_error(400, "Typing indicators are only supported in DM and group channels")
    user=db.select_data("users", ["username", "status", "share_typing"], {"id": id})[0]
    if not user["share_typing"] or user["status"]=="invisible": return jsonify({"success": True})
    if channel[0]["type"]==1:
        other=db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=? AND user_id!=?", (channel_id, id))
        if other and (db.exists("blocks", {"blocker_id": other[0]["user_id"], "blocked_id": id}) or db.exists("blocks", {"blocker_id": id, "blocked_id": other[0]["user_id"]})): return jsonify({"success": True})
    typing_signal(channel_id, id, user["username"])
    return jsonify({"success": True})
