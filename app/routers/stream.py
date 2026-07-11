import asyncio
import json
from threading import Lock, Thread
from starlette.responses import StreamingResponse
from app.router import Router
from app.api_utils import logged_in, sliding_window_rate_limiter, timestamp, perm, has_permission
from app.config import generate, config
from app.db import SQLite
from app.push import deliver_push

stream_bp=Router("stream")

streams={}
streams_lock=Lock()
call_empty_since={}
call_sweep_lock=Lock()
CALL_INACTIVITY_MS=180000

def _push(stream_data, event_data):
    loop=stream_data["loop"]
    queue=stream_data["queue"]
    try: loop.call_soon_threadsafe(queue.put_nowait, event_data)
    except RuntimeError: raise

def emit(event_type, data, conditions=None):
    """Emit event to all matching streams with thread safety"""
    with streams_lock:
        streams_to_remove=[]
        for i, stream_data in streams.items():
            try:
                should_send=True
                if conditions:
                    if "channel_ids" in conditions:
                        required_channels=conditions["channel_ids"]
                        if not any(ch in stream_data["channel_ids"] for ch in required_channels):
                            should_send=False
                    if "user_id" in conditions and stream_data["user_id"] not in conditions["user_id"]:
                        should_send=False
                    if "exclude_user" in conditions and stream_data["user_id"]==conditions["exclude_user"]:
                        should_send=False
                if should_send:
                    with stream_data["lock"]:
                        event_data={
                            "event": event_type,
                            "data": data,
                            "timestamp": timestamp(True)
                        }
                        _push(stream_data, event_data)
            except:
                streams_to_remove.append(i)
        for i in streams_to_remove:
            del streams[i]

def get_presence_recipients(user_id, db, incoming=False):
    """User ids in a presence relationship with user_id: sharing a non-DM channel, or a DM where the visible side has sent a message; never across a block in either direction. incoming=False returns who may see user_id (user_id authored the DM message); incoming=True returns whom user_id may see (the peer authored)."""
    author="m2.user_id" if incoming else "m1.user_id"
    rows=db.execute_raw_sql(f"SELECT DISTINCT m2.user_id FROM members m1 JOIN members m2 ON m1.channel_id=m2.channel_id JOIN channels c ON c.id=m1.channel_id WHERE m1.user_id=? AND m2.user_id!=? AND (c.type!=1 OR EXISTS (SELECT 1 FROM messages msg WHERE msg.channel_id=c.id AND msg.user_id={author})) AND NOT EXISTS (SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=m2.user_id) OR (blocker_id=m2.user_id AND blocked_id=?))", (user_id, user_id, user_id, user_id))
    return [row["user_id"] for row in rows]

def can_see_presence(viewer, target, db):
    """Whether viewer may currently see target's presence: a shared non-DM channel, or a DM where target has sent a message; never across a block in either direction"""
    if db.exists("blocks", {"blocker_id": viewer, "blocked_id": target}) or db.exists("blocks", {"blocker_id": target, "blocked_id": viewer}): return False
    return bool(db.execute_raw_sql("SELECT 1 FROM members m1 JOIN members m2 ON m1.channel_id=m2.channel_id JOIN channels c ON c.id=m1.channel_id WHERE m1.user_id=? AND m2.user_id=? AND (c.type!=1 OR EXISTS (SELECT 1 FROM messages msg WHERE msg.channel_id=c.id AND msg.user_id=?)) LIMIT 1", (viewer, target, target)))

def _presence_to(viewer, target, db):
    """Push target's current presence (or a removal) to viewer's live streams based on current visibility. No-op if viewer is not connected."""
    with streams_lock:
        if not any(s["user_id"]==viewer for s in streams.values()): return
    target_data=db.select_data("users", ["username", "status", "last_seen", "share_last_seen"], {"id": target})
    if not target_data: return
    target_data=target_data[0]
    if not can_see_presence(viewer, target, db): return emit("presence_remove", {"username": target_data["username"]}, {"user_id": [viewer]})
    with streams_lock: online=any(s["user_id"]==target for s in streams.values())
    status=target_data["status"] if online and target_data["status"]!="invisible" else "offline"
    data={"username": target_data["username"], "status": status}
    if status=="offline" and config["presence"]["last_seen"] and target_data["share_last_seen"]: data["last_seen"]=target_data["last_seen"]
    emit("presence_update", data, {"user_id": [viewer]})

def presence_pair_sync(user_a, user_b, db):
    """Re-evaluate presence visibility between two users in both directions and push live add/remove. Use when their relationship changes (DM first message, block, unblock)."""
    if not config["presence"]["enabled"]: return
    _presence_to(user_a, user_b, db)
    _presence_to(user_b, user_a, db)

def presence_channel_sync(channel_id, user_id, db):
    """Refresh presence between user_id and every other member of a channel after a join or leave so visibility updates without a reload"""
    if not config["presence"]["enabled"]: return
    for row in db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=? AND user_id!=?", (channel_id, user_id)): presence_pair_sync(user_id, row["user_id"], db)

def presence_broadcast(user_id, db):
    """Broadcast a user's effective presence to everyone sharing a channel with them"""
    if not config["presence"]["enabled"]: return
    user_data=db.select_data("users", ["username", "status", "last_seen", "share_last_seen"], {"id": user_id})
    if not user_data: return
    user_data=user_data[0]
    with streams_lock: connected=any(s["user_id"]==user_id for s in streams.values())
    status=user_data["status"] if connected and user_data["status"]!="invisible" else "offline"
    recipients=get_presence_recipients(user_id, db)
    if not recipients: return
    data={"username": user_data["username"], "status": status}
    if status=="offline" and config["presence"]["last_seen"] and user_data["share_last_seen"]: data["last_seen"]=user_data["last_seen"]
    emit("presence_update", data, {"user_id": recipients})

def typing_signal(channel_id, user_id, username):
    """Emit an ephemeral typing event to the other members of a channel"""
    emit("typing", {"channel_id": channel_id, "username": username}, {"channel_ids": [channel_id], "exclude_user": user_id})

def notify_offline_members(channel_id, author_id, db):
    """Web Push to channel members with no live connection. Payload carries only sender/channel (content is E2E, server can't read it)"""
    if not config["push"]["enabled"]: return
    channel=db.select_data("channels", ["type", "name"], {"id": channel_id})
    if not channel: return
    members=db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=? AND user_id!=?", (channel_id, author_id))
    with streams_lock: online=set(s["user_id"] for s in streams.values())
    offline=[m["user_id"] for m in members if m["user_id"] not in online]
    if not offline: return
    if channel[0]["type"]==1:
        author=db.select_data("users", ["username"], {"id": author_id})
        title=author[0]["username"] if author else "Holt"
    else:
        title=channel[0]["name"] or "Holt"
    deliver_push(offline, {"title": title, "channel_id": channel_id, "type": "message"})

def message_sent(channel_id, message_data, user_id, db):
    """Emit message sent event"""
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if not channel_data:
        return

    channel_type=channel_data[0]["type"]
    channel_permissions=channel_data[0]["permissions"]

    if channel_type==3:
        member_rows=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))

        manage_users=[]
        regular_users=[]

        for row in member_rows:
            member_user_id=row["user_id"]
            member_permissions=row["permissions"]

            if (has_permission(member_permissions, perm.send_messages, channel_permissions) or
                has_permission(member_permissions, perm.manage_members, channel_permissions) or
                has_permission(member_permissions, perm.manage_permissions, channel_permissions)):
                manage_users.append(member_user_id)
            else:
                regular_users.append(member_user_id)

        if manage_users:
            emit("message_sent", {
                "channel_id": channel_id,
                "message": message_data
            }, {
                "user_id": manage_users
            })

        if regular_users:
            message_data_no_author=dict(message_data)
            message_data_no_author["user"]=None
            message_data_no_author["signature"]=None
            message_data_no_author["signed_timestamp"]=None
            emit("message_sent", {
                "channel_id": channel_id,
                "message": message_data_no_author
            }, {
                "user_id": regular_users
            })
    else:
        emit("message_sent", {
            "channel_id": channel_id,
            "message": message_data
        }, {
            "channel_ids": [channel_id],
        })
    notify_offline_members(channel_id, user_id, db)

def message_edited(channel_id, message_data, user_id, db):
    """Emit message edited event"""
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if not channel_data:
        return

    channel_type=channel_data[0]["type"]
    channel_permissions=channel_data[0]["permissions"]

    if channel_type==3:
        member_rows=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))

        manage_users=[]
        regular_users=[]

        for row in member_rows:
            member_user_id=row["user_id"]
            member_permissions=row["permissions"]

            if (has_permission(member_permissions, perm.send_messages, channel_permissions) or
                has_permission(member_permissions, perm.manage_members, channel_permissions) or
                has_permission(member_permissions, perm.manage_permissions, channel_permissions)):
                manage_users.append(member_user_id)
            else:
                regular_users.append(member_user_id)

        if manage_users:
            emit("message_edited", {
                "channel_id": channel_id,
                "message": message_data
            }, {
                "user_id": manage_users
            })

        if regular_users:
            message_data_no_author=dict(message_data)
            message_data_no_author["user"]=None
            message_data_no_author["signature"]=None
            message_data_no_author["signed_timestamp"]=None
            message_data_no_author["reactions"]=[{**r, "user": None, "signature": None, "signed_timestamp": None} for r in (message_data.get("reactions") or [])]
            emit("message_edited", {
                "channel_id": channel_id,
                "message": message_data_no_author
            }, {
                "user_id": regular_users
            })
    else:
        emit("message_edited", {
            "channel_id": channel_id,
            "message": message_data
        }, {
            "channel_ids": [channel_id]
        })

def message_deleted(channel_id, message_id, user_id):
    """Emit message deleted event"""
    emit("message_deleted", {
        "channel_id": channel_id,
        "message_id": message_id
    }, {
        "channel_ids": [channel_id]
    })

def reaction_add(channel_id, message_id, reaction_data, db):
    """Emit reaction added event"""
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if not channel_data:
        return

    channel_type=channel_data[0]["type"]
    channel_permissions=channel_data[0]["permissions"]

    if channel_type==3:
        member_rows=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))

        manage_users=[]
        regular_users=[]

        for row in member_rows:
            member_user_id=row["user_id"]
            member_permissions=row["permissions"]

            if (has_permission(member_permissions, perm.send_messages, channel_permissions) or
                has_permission(member_permissions, perm.manage_members, channel_permissions) or
                has_permission(member_permissions, perm.manage_permissions, channel_permissions)):
                manage_users.append(member_user_id)
            else:
                regular_users.append(member_user_id)

        if manage_users:
            emit("reaction_add", {
                "channel_id": channel_id,
                "message_id": message_id,
                "reaction": reaction_data
            }, {
                "user_id": manage_users
            })

        if regular_users:
            reaction_data_no_author=dict(reaction_data)
            reaction_data_no_author["user"]=None
            reaction_data_no_author["signature"]=None
            reaction_data_no_author["signed_timestamp"]=None
            emit("reaction_add", {
                "channel_id": channel_id,
                "message_id": message_id,
                "reaction": reaction_data_no_author
            }, {
                "user_id": regular_users
            })
    else:
        emit("reaction_add", {
            "channel_id": channel_id,
            "message_id": message_id,
            "reaction": reaction_data
        }, {
            "channel_ids": [channel_id]
        })

def reaction_remove(channel_id, message_id, reaction_id, db):
    """Emit reaction removed event"""
    emit("reaction_remove", {
        "channel_id": channel_id,
        "message_id": message_id,
        "reaction_id": reaction_id
    }, {
        "channel_ids": [channel_id]
    })

def channel_added(user_id, channel_data, db=None):
    """Emit channel added event and update user's channel_ids"""
    # Update the user's channel_ids in their active streams
    with streams_lock:
        for i, stream_data in streams.items():
            if stream_data["user_id"]==user_id:
                with stream_data["lock"]:
                    if channel_data["id"] not in stream_data["channel_ids"]:
                        stream_data["channel_ids"].append(channel_data["id"])

    # Check if user has manage_permissions to include channel_permissions
    if db:
        member_data=db.select_data("members", ["permissions"], {"channel_id": channel_data["id"], "user_id": user_id})
        member_permissions=member_data[0]["permissions"] if member_data else None
        effective_permissions=member_permissions if member_permissions is not None else channel_data.get("permissions", 0)

        if has_permission(member_permissions, perm.manage_permissions, channel_data.get("permissions", 0)):
            db_channel_data=db.select_data("channels", ["permissions"], {"id": channel_data["id"]})
            actual_channel_permissions=db_channel_data[0]["permissions"] if db_channel_data else 0
            channel_with_perms=dict(channel_data)
            channel_with_perms["channel_permissions"]=actual_channel_permissions
            emit("channel_added", {
                "channel": channel_with_perms
            }, {
                "user_id": [user_id]
            })
            return

    emit("channel_added", {
        "channel": channel_data
    }, {
        "user_id": [user_id]
    })

def channel_edited(channel_id, channel_data, db):
    """Emit channel edited event with effective permissions per user"""
    member_rows=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
    for row in member_rows:
        user_id=row["user_id"]
        effective_permissions=row["permissions"] if row["permissions"] is not None else channel_data["permissions"]
        user_channel=dict(channel_data)
        user_channel["permissions"]=effective_permissions

        # Include channel_permissions if user has manage_permissions
        if has_permission(row["permissions"], perm.manage_permissions, channel_data["permissions"]):
            user_channel["channel_permissions"]=channel_data["permissions"]

        emit("channel_edited", {
            "channel_id": channel_id,
            "channel": user_channel
        }, {
            "user_id": [user_id]
        })

def channel_deleted(channel_id, db):
    """Emit channel deleted event and update users' channel_ids"""
    member_data=db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=?", (channel_id,))
    user_ids=[row["user_id"] for row in member_data]

    if user_ids:
        emit("channel_deleted", {
            "channel_id": channel_id
        }, {
            "user_id": user_ids
        })

    # Update channel_ids for all affected users' streams
    with streams_lock:
        for i, stream_data in streams.items():
            if stream_data["user_id"] in user_ids:
                with stream_data["lock"]:
                    if channel_id in stream_data["channel_ids"]:
                        stream_data["channel_ids"].remove(channel_id)

def update_channel_keys_on_member_change(channel_id, db):
    """Expire all live keys for the channel immediately when members change, forcing a rotation the departed member can't decrypt"""
    db.execute_raw_sql(
        "UPDATE channels_keys_info SET expires_at=0 WHERE channel_id=? AND expires_at>=?",
        (channel_id, timestamp())
    )

def _emit_member_event_with_channel_perms(event_type, event_data, channel_id, member_user_id, db):
    """Helper function to emit member events with permission filtering for channel type 3"""
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if channel_data and channel_data[0]["type"]==3:
        # For channel type 3, only send to users in this channel with manage_channel or manage_permissions
        channel_permissions=channel_data[0]["permissions"]
        all_members=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
        user_ids=[]
        for member in all_members:
            if has_permission(member["permissions"], perm.manage_permissions, channel_permissions):
                user_ids.append(member["user_id"])

        # Always include the member who is joining/leaving
        if member_user_id not in user_ids:
            user_ids.append(member_user_id)

        emit(event_type, event_data, {"user_id": user_ids})
    else:
        emit(event_type, event_data, {"channel_ids": [channel_id]})

def member_join(channel_id, user_data, db):
    """Emit member join event and update user's channel_ids"""
    user_id=user_data["id"]

    # Expire all live keys so the next send rotates to one the changed member set can't decrypt
    update_channel_keys_on_member_change(channel_id, db)

    # Update the user's channel_ids in their active streams
    with streams_lock:
        for i, stream_data in streams.items():
            if stream_data["user_id"]==user_id:
                with stream_data["lock"]:
                    if channel_id not in stream_data["channel_ids"]:
                        stream_data["channel_ids"].append(channel_id)

    # Get member's permissions for the event
    member_data=db.select_data("members", ["permissions"], {"channel_id": channel_id, "user_id": user_id})
    member_permissions=member_data[0]["permissions"] if member_data else None

    # Create user data without id for the event
    user_event_data={k: v for k, v in user_data.items() if k!="id"}

    # Emit to users with manage permissions (include permissions data)
    channel_data=db.select_data("channels", ["type", "permissions"], {"id": channel_id})
    if channel_data:
        channel_permissions=channel_data[0]["permissions"]
        effective_permissions=member_permissions if member_permissions is not None else channel_permissions

        # Get all members and filter with has_permission
        all_members=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
        manage_user_ids=[]
        non_manage_user_ids=[]
        for member in all_members:
            if has_permission(member["permissions"], perm.manage_permissions, channel_permissions):
                manage_user_ids.append(member["user_id"])
            else:
                non_manage_user_ids.append(member["user_id"])

        # Send event with permissions to manage users
        if manage_user_ids:
            emit("member_join", {
                "channel_id": channel_id,
                "user": user_event_data,
                "permissions": effective_permissions
            }, {"user_id": manage_user_ids})

        # Send event without permissions to other users
        if non_manage_user_ids:
            emit("member_join", {
                "channel_id": channel_id,
                "user": user_event_data
            }, {"user_id": non_manage_user_ids})
    else:
        # Fallback to original behavior
        _emit_member_event_with_channel_perms("member_join", {
            "channel_id": channel_id,
            "user": user_event_data
        }, channel_id, user_id, db)

    presence_channel_sync(channel_id, user_id, db)

def member_leave(channel_id, user_data, db):
    """Emit member leave event and update user's channel_ids"""
    user_id=user_data["id"]

    # Expire all live keys so the next send rotates to one the changed member set can't decrypt
    update_channel_keys_on_member_change(channel_id, db)

    # Create user data without id for the event
    user_event_data={k: v for k, v in user_data.items() if k!="id"}

    _emit_member_event_with_channel_perms("member_leave", {
        "channel_id": channel_id,
        "user": user_event_data
    }, channel_id, user_id, db)

    # Update the user's channel_ids in their active streams
    with streams_lock:
        for i, stream_data in streams.items():
            if stream_data["user_id"]==user_id:
                with stream_data["lock"]:
                    if channel_id in stream_data["channel_ids"]:
                        stream_data["channel_ids"].remove(channel_id)

    presence_channel_sync(channel_id, user_id, db)

def member_info_changed(user_id, user_data, db, old_username=None):
    """Emit member info changed event (only once per member across all channels). old_username is set when the username itself changed (e.g. anonymization) so clients can locate the old entry, since ids are never sent to clients"""
    # Get all channels where this user is a member
    user_channels=db.execute_raw_sql("SELECT c.id as channel_id, c.type FROM channels c JOIN members m ON c.id=m.channel_id WHERE m.user_id=?", (user_id,))
    channel_ids=[row["channel_id"] for row in user_channels]

    if channel_ids:
        # Create user data without id for the event
        user_event_data={k: v for k, v in user_data.items() if k!="id"}
        payload={"user": user_event_data, "channels": channel_ids}
        if old_username: payload["old_username"]=old_username

        # Check if all channels are type 3
        all_type_3=all(row["type"]==3 for row in user_channels)

        if all_type_3:
            # All mutual channels are type 3, use permission-based filtering
            # Get all users with manage_channel or manage_permissions across all these channels
            permitted_users_set=set()
            for row in user_channels:
                channel_id=row["channel_id"]
                channel_data=db.select_data("channels", ["permissions"], {"id": channel_id})
                if channel_data:
                    channel_permissions=channel_data[0]["permissions"]
                    channel_members=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
                    for member in channel_members:
                        if has_permission(member["permissions"], perm.manage_permissions, channel_permissions):
                            permitted_users_set.add(member["user_id"])

            if permitted_users_set:
                emit("member_info_changed", payload, {"user_id": list(permitted_users_set)})
        else:
            # Normal behavior for mixed or non-type-3 channels
            emit("member_info_changed", payload, {"channel_ids": channel_ids})

def presence_drop_username(user_id, old_username, db):
    """Tell everyone who could see this user that the given (old) username is gone. Used on anonymization since the username changes and presence is keyed by username on clients"""
    if not config["presence"]["enabled"]: return
    recipients=get_presence_recipients(user_id, db)
    if recipients: emit("presence_remove", {"username": old_username}, {"user_id": recipients})

def member_perms_changed(channel_id, user_id, username, permissions, db):
    channel_data=db.select_data("channels", ["permissions"], {"id": channel_id})
    channel_permissions=channel_data[0]["permissions"] if channel_data else 0
    effective_permissions=permissions if permissions is not None else channel_permissions

    # Get all members and filter with has_permission
    all_members=db.execute_raw_sql("SELECT user_id, permissions FROM members WHERE channel_id=?", (channel_id,))
    manage_user_ids=[]
    for member in all_members:
        if has_permission(member["permissions"], perm.manage_permissions, channel_permissions):
            manage_user_ids.append(member["user_id"])

    # Always include the target user
    if user_id not in manage_user_ids:
        manage_user_ids.append(user_id)

    emit("member_perms_changed", {
        "username": username,
        "channel_id": channel_id,
        "permissions": effective_permissions
    }, {
        "user_id": manage_user_ids
    })

def dm_unhide(channel_id, user_id, db):
    """Emit channel_added and member_join events when a DM is unhidden, only to the specific user"""
    # Get the other user in the DM
    other_user=db.execute_raw_sql("SELECT user_id FROM members WHERE channel_id=? AND user_id!=?", (channel_id, user_id))
    if not other_user: return

    other_user_id=other_user[0]["user_id"]

    # Get user data
    other_user_data=db.select_data("users", ["username", "display_name", "pfp"], {"id": other_user_id})[0]
    current_user_data=db.select_data("users", ["username", "display_name", "pfp"], {"id": user_id})[0]

    # Emit channel_added event to the user who unhid the channel (showing other user's info)
    channel_data={
        "id": channel_id,
        "name": other_user_data["display_name"] if other_user_data["display_name"] else other_user_data["username"],
        "pfp": other_user_data["pfp"],
        "type": 1,
        "permissions": perm.send_messages,
        "member_count": 2
    }
    if other_user_data["display_name"]:
        channel_data["username"]=other_user_data["username"]
    channel_added(user_id, channel_data, db)

    # Emit member_join event only to the user who unhid the channel
    user_event_data={k: v for k, v in current_user_data.items() if k!="id"}
    emit("member_join", {
        "channel_id": channel_id,
        "user": user_event_data
    }, {"user_id": [user_id]})

def call_start(channel_id, started_by_username, db):
    """Emit call start event"""
    emit("call_start", {
        "channel_id": channel_id,
        "started_by": started_by_username,
        "timestamp": timestamp(True)
    }, {
        "channel_ids": [channel_id]
    })

def call_join(channel_id, user_data, db):
    """Emit call join event"""
    emit("call_join", {
        "channel_id": channel_id,
        "user": {
            "id": user_data["id"],
            "username": user_data["username"],
            "display": user_data["display_name"],
            "pfp": user_data["pfp"],
            "public": user_data["public_key"]
        }
    }, {
        "channel_ids": [channel_id]
    })

def call_left(channel_id, user_data, db):
    """Emit call left event"""
    emit("call_left", {
        "channel_id": channel_id,
        "user": {
            "id": user_data["id"],
            "username": user_data["username"],
            "display": user_data["display_name"],
            "pfp": user_data["pfp"]
        }
    }, {
        "channel_ids": [channel_id]
    })

def call_signal(channel_id, from_user_id, signal_type, signal_data, target, db):
    """Emit WebRTC signaling to a specific participant, or broadcast settings to the channel"""
    payload={"channel_id": channel_id, "from_user": from_user_id, "type": signal_type, "data": signal_data}
    if target:
        emit("call_signal", payload, {"user_id": [target]})
    else:
        emit("call_signal", payload, {"channel_ids": [channel_id], "exclude_user": from_user_id})

def _end_call(channel_id, db):
    """End a call: emit call_left for any remaining participants and delete the call and its participants"""
    participants=db.execute_raw_sql("SELECT u.id, u.username, u.display_name, u.pfp FROM call_participants cp JOIN users u ON cp.user_id=u.id WHERE cp.channel_id=? AND cp.left_at IS NULL", (channel_id,))
    for user_data in participants: call_left(channel_id, user_data, db)
    db.delete_data("calls", {"channel_id": channel_id})
    db.execute_raw_sql("UPDATE call_history SET ended_at=? WHERE channel_id=? AND ended_at IS NULL", (timestamp(True), channel_id))

def _call_sweep():
    """Periodically end calls with no genuinely-present participant (no live stream) for >=3 minutes. A call that regains a connected participant resets its timer."""
    from app.config import stopping
    while not stopping.is_set():
        db=SQLite()
        try:
            now=timestamp(True)
            active_calls=db.execute_raw_sql("SELECT channel_id FROM calls")
            with streams_lock: online=set(s["user_id"] for s in streams.values())
            active_channel_ids=set()
            for call in active_calls:
                channel_id=call["channel_id"]
                active_channel_ids.add(channel_id)
                participants=db.execute_raw_sql("SELECT user_id FROM call_participants WHERE channel_id=? AND left_at IS NULL", (channel_id,))
                connected=any(p["user_id"] in online for p in participants)
                if connected:
                    call_empty_since.pop(channel_id, None)
                elif channel_id not in call_empty_since:
                    call_empty_since[channel_id]=now
                elif now-call_empty_since[channel_id]>=CALL_INACTIVITY_MS:
                    _end_call(channel_id, db)
                    call_empty_since.pop(channel_id, None)
            for channel_id in [c for c in call_empty_since if c not in active_channel_ids]: call_empty_since.pop(channel_id, None)
        finally: db.close()
        stopping.wait(30)

def purge_all_calls():
    """Clear any calls left over from a previous run so a call never survives a backend restart."""
    db=SQLite()
    try:
        db.execute_raw_sql("DELETE FROM call_participants")
        db.execute_raw_sql("DELETE FROM calls")
    finally: db.close()

def start_call_sweep():
    purge_all_calls()
    Thread(target=_call_sweep, daemon=True).start()

def _message_expiry_sweep():
    """Periodically delete messages whose expires_at has passed and notify open clients"""
    from app.config import stopping
    while not stopping.is_set():
        db=SQLite()
        try:
            expired=db.execute_raw_sql("SELECT id, channel_id FROM messages WHERE expires_at IS NOT NULL AND expires_at<=?", (timestamp(True),))
            for msg in expired:
                db.delete_data("messages", {"id": msg["id"]})
                message_deleted(msg["channel_id"], msg["id"], None)
            if expired:
                db.cleanup_unused_files()
                db.cleanup_unused_keys()
        finally: db.close()
        stopping.wait(5)

def start_message_expiry_sweep():
    Thread(target=_message_expiry_sweep, daemon=True).start()

def _stream_setup(db, session_id, id, loop):
    """Synchronous connection setup: build snapshot, register stream, return stream_data + client id."""
    channel_ids=db.execute_raw_sql("""
        SELECT c.id FROM channels c
        JOIN members m ON c.id=m.channel_id
        WHERE m.user_id=?
    """, (id,))
    channel_ids=[row["id"] for row in channel_ids]
    active_call_events=[]
    if channel_ids:
        placeholders=", ".join(["?"]*len(channel_ids))
        active_call_rows=db.execute_raw_sql(f"""
            SELECT c.channel_id, c.started_at, u.username FROM calls c
            JOIN users u ON c.started_by=u.id
            WHERE c.channel_id IN ({placeholders})
        """, tuple(channel_ids))
        for row in active_call_rows:
            active_call_events.append({
                "event": "call_start",
                "data": {
                    "channel_id": row["channel_id"],
                    "started_by": row["username"],
                    "timestamp": row["started_at"]
                }
            })

    presence_snapshot=[]
    if config["presence"]["enabled"]:
        visible=get_presence_recipients(id, db, True)
        if visible:
            with streams_lock: online=set(s["user_id"] for s in streams.values())
            placeholders=", ".join(["?"]*len(visible))
            presence_rows=db.execute_raw_sql(f"SELECT id, username, status, last_seen, share_last_seen FROM users WHERE id IN ({placeholders})", tuple(visible))
            for row in presence_rows:
                status=row["status"] if (row["id"] in online and row["status"]!="invisible") else "offline"
                data={"username": row["username"], "status": status}
                if status=="offline" and config["presence"]["last_seen"] and row["share_last_seen"]: data["last_seen"]=row["last_seen"]
                presence_snapshot.append({"event": "presence_update", "data": data})
    queue=asyncio.Queue()
    for event in active_call_events+presence_snapshot:
        queue.put_nowait(event)
    stream_data={
        "channel_ids": channel_ids,
        "user_id": id,
        "queue": queue,
        "loop": loop,
        "lock": Lock()
    }
    client=generate()
    with streams_lock:
        first_connect=not any(s["user_id"]==id for s in streams.values())
        streams[client]=stream_data
    if first_connect: presence_broadcast(id, db)
    return stream_data, client

def _stream_cleanup(client, id):
    with streams_lock:
        if client in streams: del streams[client]
        last_disconnect=not any(s["user_id"]==id for s in streams.values())
    if last_disconnect:
        pdb=SQLite()
        try:
            for row in pdb.execute_raw_sql("SELECT channel_id FROM call_participants WHERE user_id=? AND left_at IS NULL", (id,)):
                pdb.update_data("call_participants", {"left_at": timestamp(True)}, {"channel_id": row["channel_id"], "user_id": id})
                user_data=pdb.select_data("users", ["id", "username", "display_name", "pfp"], {"id": id})[0]
                call_left(row["channel_id"], user_data, pdb)
            if config["presence"]["enabled"]:
                user_status=pdb.select_data("users", ["status", "share_last_seen"], {"id": id})
                if user_status and user_status[0]["status"]!="invisible" and user_status[0]["share_last_seen"]: pdb.update_data("users", {"last_seen": timestamp(True)}, {"id": id})
                presence_broadcast(id, pdb)
        finally: pdb.close()

@stream_bp.route("/stream")
@logged_in(True)
@sliding_window_rate_limiter(limit=10, window=60, user_limit=5)
def stream(db:SQLite, session_id, id):
    from app.compat import event_loop_var
    loop=event_loop_var.get()
    stream_data, client=_stream_setup(db, session_id, id, loop)
    queue=stream_data["queue"]
    async def generator():
        try:
            yield ": heartbeat\n\n"
            next_heartbeat=timestamp()+10
            session_check_time=timestamp()+60
            while True:
                current_time=timestamp()
                if current_time>=session_check_time:
                    valid=await asyncio.to_thread(_session_valid, session_id)
                    if not valid:
                        yield "event: error\ndata: {\"error\": \"Invalid_session\"}\n\n"
                        break
                    session_check_time=current_time+60
                timeout=min(next_heartbeat, session_check_time)-current_time
                if timeout<0: timeout=0
                try:
                    event=await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if timestamp()>=next_heartbeat:
                        yield ": heartbeat\n\n"
                        next_heartbeat=timestamp()+10
                    continue
                event_str=json.dumps(event["data"])
                yield f"event: {event['event']}\ndata: {event_str}\n\n"
                if timestamp()>=next_heartbeat:
                    yield ": heartbeat\n\n"
                    next_heartbeat=timestamp()+10
        except Exception:
            yield "event: error\ndata: {\"error\": \"connection_error\"}\n\n"
        finally:
            await asyncio.to_thread(_stream_cleanup, client, id)
    resp=StreamingResponse(generator(), media_type="text/event-stream")
    resp.headers["Cache-Control"]="no-cache"
    resp.headers["X-Accel-Buffering"]="no"
    resp.headers["Connection"]="keep-alive"
    return resp

def _session_valid(session_id):
    db=SQLite()
    try:
        if session_id.startswith("bot:"): return db.exists("bot_tokens", {"id": session_id[4:]})
        return db.exists("session", {"id": session_id})
    finally: db.close()
