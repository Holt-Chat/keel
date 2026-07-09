import sqlite3
import json
import logging
import os
import hashlib
import time
import math
from typing import List, Dict, Any, Union, Tuple, Optional
from app.config import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger=logging.getLogger(__name__)

class SQLite:
    def __init__(self, db_path: str=config["data_dir"]["database"]):
        self.db_path=db_path
        self._conn: Optional[sqlite3.Connection]=None
        self._cursor: Optional[sqlite3.Cursor]=None
        self._in_context: bool=False
        self._connect()

    def _prepare_value_for_db(self, val: Any) -> Any:
        if val is None:
            return None
        elif isinstance(val, (dict, list)):
            return json.dumps(val)
        elif isinstance(val, bool):
            return 1 if val else 0
        return val

    def _prepare_conditions_for_db(self, conditions: Dict[str, Any]) -> Tuple[List[str], List[Any]]:
        where_clauses=[]
        params=[]
        for col, val in conditions.items():
            if isinstance(val, list) and val:
                placeholders=', '.join(['?'] * len(val))
                where_clauses.append(f"{col} IN ({placeholders})")
                params.extend([self._prepare_value_for_db(v) for v in val])
            else:
                where_clauses.append(f"{col}=?")
                params.append(self._prepare_value_for_db(val))
        return where_clauses, params

    def _connect(self) -> None:
        if self._conn is None:
            try:
                self._conn=sqlite3.connect(self.db_path)
                self._conn.row_factory=sqlite3.Row
                self._cursor=self._conn.cursor()
                self._cursor.execute("PRAGMA journal_mode=WAL;")
                self._cursor.execute("PRAGMA foreign_keys=ON;")
                self._cursor.execute("PRAGMA cache_size=32768;")
            except sqlite3.Error as e:
                logger.error(f"Failed to connect to database {self.db_path}: {e}")
                raise

    def execute(self, sql_query: str, params: Union[Tuple, List]=()) -> sqlite3.Cursor:
        self._connect()
        try:
            self._cursor.execute(sql_query, tuple(params))
            return self._cursor
        except sqlite3.IntegrityError as e:
            logger.error(f"Integrity Error executing SQL: '{sql_query}' with params {params}. Error: {e}")
            self._conn.rollback()
            raise
        except sqlite3.OperationalError as e:
            logger.error(f"Operational Error executing SQL: '{sql_query}' with params {params}. Error: {e}")
            raise
        except sqlite3.Error as e:
            logger.error(f"Database Error executing SQL: '{sql_query}' with params {params}. Error: {e}")
            raise

    def commit(self) -> None:
        if self._conn:
            try:
                self._conn.commit()
                logger.debug("Transaction committed.")
            except sqlite3.Error as e:
                logger.error(f"Error committing transaction: {e}")
                raise
        else:
            logger.warning("No active connection to commit.")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn=None
            self._cursor=None

    def __enter__(self):
        self._connect()
        self._cursor.execute("BEGIN TRANSACTION")
        self._in_context=True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._in_context=False
        if exc_type:
            logger.error(f"An error occurred within the database context: {exc_type.__name__}: {exc_val}")
            if self._conn:
                self._conn.rollback()
                logger.info("Transaction rolled back due to an error.")
        else:
            if self._conn:
                self.commit()
        self.close()

    def create_table(self, table_name: str, columns: Dict[str, str]) -> bool:
        column_defs=[f"{col_name} {col_type}" for col_name, col_type in columns.items()]
        columns_sql=", ".join(column_defs)
        create_sql=f"CREATE TABLE IF NOT EXISTS {table_name} ({columns_sql})"
        try:
            self.execute(create_sql)
            if not self._in_context:
                self.commit()
            return True
        except sqlite3.Error:
            return False

    def add_column(self, table_name: str, column_name: str, column_type: str, default_value: Optional[Any]=None) -> bool:
        column_def=f"{column_name} {column_type}"
        if default_value is not None:
            prepared_default=self._prepare_value_for_db(default_value)
            if isinstance(prepared_default, str):
                column_def += f" DEFAULT '{prepared_default}'"
            else:
                column_def += f" DEFAULT {prepared_default}"
        alter_sql=f"ALTER TABLE {table_name} ADD COLUMN {column_def}"
        try:
            self.execute(alter_sql)
            if not self._in_context:
                self.commit()
            return True
        except sqlite3.Error:
            table_info=self.get_table_info(table_name)
            if any(col['name']==column_name for col in table_info):
                logger.info(f"Column '{column_name}' already exists in table '{table_name}'.")
                return True
            return False

    def insert_data(self, table_name: str, data: Dict[str, Any]) -> Optional[int]:
        if not data:
            logger.warning(f"No data provided for insertion into '{table_name}'.")
            return None
        columns=list(data.keys())
        placeholders=", ".join(["?"] * len(columns))
        values=[self._prepare_value_for_db(data[col]) for col in columns]
        columns_str=", ".join(columns)
        insert_sql=f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders})"
        cursor=self.execute(insert_sql, values)
        result=cursor.lastrowid
        if result is not None and not self._in_context:
            self.commit()
        return result

    def select_data(self, table_name: str, columns: List[str]=['*'], conditions: Optional[Dict[str, Any]]=None,
                    order_by: Optional[str]=None, limit: Optional[int]=None, offset: Optional[int]=None) -> List[Dict[str, Any]]:
        select_cols=", ".join(columns)
        query=f"SELECT {select_cols} FROM {table_name}"
        params: List[Any]=[]
        if conditions:
            where_clauses, params=self._prepare_conditions_for_db(conditions)
            query += " WHERE " + " AND ".join(where_clauses)
        if order_by:
            query += f" ORDER BY {order_by}"
        if limit is not None:
            query += f" LIMIT {limit}"
        if offset is not None:
            query += f" OFFSET {offset}"
        cursor=self.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def update_data(self, table_name: str, set_data: Dict[str, Any], conditions: Dict[str, Any]) -> int:
        if not set_data or not conditions:
            logger.warning(f"Update operation for '{table_name}' requires both 'set_data' and 'conditions'.")
            return 0
        set_clauses=[]
        set_values=[]
        for col, val in set_data.items():
            set_clauses.append(f"{col}=?")
            set_values.append(self._prepare_value_for_db(val))
        where_clauses, where_values=self._prepare_conditions_for_db(conditions)
        update_sql=f"UPDATE {table_name} SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}"
        params=set_values + where_values
        cursor=self.execute(update_sql, params)
        result=cursor.rowcount
        if not self._in_context:
            self.commit()
        return result

    def delete_data(self, table_name: str, conditions: Dict[str, Any]) -> int:
        if not conditions:
            logger.warning(f"Delete operation for '{table_name}' requires 'conditions' to prevent accidental full table deletion.")
            return 0
        where_clauses, where_values=self._prepare_conditions_for_db(conditions)
        delete_sql=f"DELETE FROM {table_name} WHERE {' AND '.join(where_clauses)}"
        params=where_values
        cursor=self.execute(delete_sql, params)
        result=cursor.rowcount
        if not self._in_context:
            self.commit()
        return result

    def drop_table(self, table_name: str) -> bool:
        drop_sql=f"DROP TABLE IF EXISTS {table_name}"
        try:
            self.execute(drop_sql)
            if not self._in_context:
                self.commit()
            return True
        except sqlite3.Error:
            return False

    def get_table_info(self, table_name: str) -> List[Dict[str, Any]]:
        query=f"PRAGMA table_info({table_name})"
        cursor=self.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    def get_all_table_names(self) -> List[str]:
        query="SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        cursor=self.execute(query)
        return [row['name'] for row in cursor.fetchall()]

    def exists(self, table_name: str, conditions: Dict[str, Any]) -> bool:
        if not conditions:
            logger.warning("Checking existence requires conditions.")
            return False
        where_clauses, params=self._prepare_conditions_for_db(conditions)
        query=f"SELECT 1 FROM {table_name} WHERE {' AND '.join(where_clauses)} LIMIT 1"
        cursor=self.execute(query, params)
        return cursor.fetchone() is not None

    def create_index(self, table_name: str, column_name: Union[str, List[str]], unique: bool=False) -> bool:
        if isinstance(column_name, str):
            columns_str=column_name
            index_suffix=column_name
        else:
            columns_str=", ".join(column_name)
            index_suffix="_".join(column_name)
        index_name=f"idx_{table_name}_{index_suffix}"
        unique_str="UNIQUE" if unique else ""
        index_sql=f"CREATE {unique_str} INDEX IF NOT EXISTS {index_name} ON {table_name} ({columns_str})"
        try:
            self.execute(index_sql)
            if not self._in_context:
                self.commit()
            return True
        except sqlite3.Error:
            return False

    def drop_index(self, index_name: str) -> bool:
        drop_sql=f"DROP INDEX IF EXISTS {index_name}"
        try:
            self.execute(drop_sql)
            if not self._in_context:
                self.commit()
            return True
        except sqlite3.Error:
            return False

    def get_all_indexes(self, table_name: Optional[str]=None) -> List[Dict[str, Any]]:
        query="SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'"
        params: List[Any]=[]
        if table_name:
            query += " WHERE tbl_name=?"
            params.append(table_name)
        cursor=self.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def execute_raw_sql(self, sql_query: str, params: Union[Tuple, List]=()) -> List[Dict[str, Any]]:
        cursor=self.execute(sql_query, params)
        is_write_operation=sql_query.strip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
        if is_write_operation:
            if not self._in_context:
                self.commit()
            return []
        else:
            try:
                rows=cursor.fetchall()
                return [dict(row) for row in rows] if rows else []
            except sqlite3.ProgrammingError:
                return []

    def execute_script(self, sql_script: str) -> None:
        self._connect()
        try:
            self._cursor.executescript(sql_script)
            if not self._in_context:
                self.commit()
        except sqlite3.Error as e:
            logger.error(f"Database Error executing script: {e}")
            raise

    def batch_select(self, queries: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        results=[]
        for query in queries:
            table_name=query["table"]
            columns=query.get("columns", ["*"])
            conditions=query.get("conditions")
            order_by=query.get("order_by")
            limit=query.get("limit")
            offset=query.get("offset")
            result=self.select_data(table_name, columns, conditions, order_by, limit, offset)
            results.append(result)
        return results

    def batch_exists(self, queries: List[Dict[str, Any]]) -> List[bool]:
        results=[]
        for query in queries:
            table_name=query["table"]
            conditions=query["conditions"]
            result=self.exists(table_name, conditions)
            results.append(result)
        return results

    def get_permission_data(self, user_id: int, channel_id: int, target_username: str=None) -> Dict[str, Any]:
        queries=[
            {"table": "members", "columns": ["permissions"], "conditions": {"user_id": user_id, "channel_id": channel_id}},
            {"table": "channels", "columns": ["type", "permissions"], "conditions": {"id": channel_id}}
        ]
        if target_username:
            queries.append({"table": "users", "columns": ["id"], "conditions": {"username": target_username}})

        results=self.batch_select(queries)

        data={
            "admin_member": results[0],
            "channel_data": results[1]
        }

        if target_username and len(results) > 2:
            target_user=results[2]
            data["target_user"]=target_user
            if target_user:
                target_user_id=target_user[0]["id"]
                target_member=self.select_data("members", ["permissions"], {"user_id": target_user_id, "channel_id": channel_id})
                data["target_member"]=target_member
                data["target_user_id"]=target_user_id

        return data

    def validate_user_action(self, user_id: int, channel_id: int, target_username: str, action_type: str="general") -> Dict[str, Any]:
        perm_data=self.get_permission_data(user_id, channel_id, target_username)

        if action_type in ["ban", "kick"] and "target_user_id" in perm_data:
            target_user_id=perm_data["target_user_id"]

            ban_check=self.exists("bans", {"user_id": target_user_id, "channel_id": channel_id})
            perm_data["existing_ban"]=ban_check

        return perm_data

    def cleanup_unused_files(self) -> int:
        """Remove files that are no longer referenced by any messages, users, channels, or embed assets"""
        if self._in_context:
            unused_files=self.execute_raw_sql("""
                SELECT f.id, f.file_type FROM files f
                WHERE f.id NOT IN (SELECT file_id FROM attachment_message WHERE file_id IS NOT NULL)
                AND f.id NOT IN (SELECT pfp FROM users WHERE pfp IS NOT NULL)
                AND f.id NOT IN (SELECT pfp FROM channels WHERE pfp IS NOT NULL)
                AND f.id NOT IN (SELECT file_id FROM embed_assets)
            """)
            if not unused_files: return
            file_ids=[f["id"] for f in unused_files]
            self.execute(f"DELETE FROM files WHERE id IN ({",".join(["?"] * len(file_ids))})", file_ids)
        else:
            with self:
                unused_files=self.execute_raw_sql("""
                    SELECT f.id, f.file_type FROM files f
                    WHERE f.id NOT IN (SELECT file_id FROM attachment_message WHERE file_id IS NOT NULL)
                    AND f.id NOT IN (SELECT pfp FROM users WHERE pfp IS NOT NULL)
                    AND f.id NOT IN (SELECT pfp FROM channels WHERE pfp IS NOT NULL)
                    AND f.id NOT IN (SELECT file_id FROM embed_assets)
                """)
                if not unused_files: return
                file_ids=[f["id"] for f in unused_files]
                self.execute(f"DELETE FROM files WHERE id IN ({",".join(["?"] * len(file_ids))})", file_ids)
        for file_record in unused_files:
            file_type=file_record["file_type"]
            if file_type=="attachment":
                file_path=os.path.join(config["data_dir"]["attachments"], file_record["id"])
            elif file_type=="pfp":
                file_path=os.path.join(config["data_dir"]["pfps"], f"{file_record['id']}.webp")
            else:
                continue
            if os.path.isfile(file_path):
                try: os.remove(file_path)
                except OSError as e: logger.error(f"Failed to remove {file_type} file {file_record['id']}: {e}")

    def cleanup_unused_keys(self):
        """Remove keys that are no longer referenced by any messages"""
        if self._in_context:
            self.execute("""
                DELETE FROM channels_keys_info
                WHERE key_id NOT IN (SELECT DISTINCT key FROM messages WHERE key IS NOT NULL)
                AND expires_at < ?
            """, (math.floor(time.time()*1000),))
            self.execute("""
                DELETE FROM channels_keys
                WHERE id NOT IN (SELECT key_id FROM channels_keys_info)
            """)
        else:
            with self:
                self.execute("""
                    DELETE FROM channels_keys_info
                    WHERE key_id NOT IN (SELECT DISTINCT key FROM messages WHERE key IS NOT NULL)
                    AND expires_at < ?
                """, (math.floor(time.time()*1000),))
                self.execute("""
                    DELETE FROM channels_keys
                    WHERE id NOT IN (SELECT key_id FROM channels_keys_info)
                """)

    def cleanup_stale_embed_assets(self, ttl_ms: int=3600000):
        """Remove embed asset uploads that were never attached to a sent message"""
        cutoff=math.floor(time.time()*1000)-ttl_ms
        if self._in_context:
            self.execute("DELETE FROM embed_assets WHERE message_id IS NULL AND created_at<?", (cutoff,))
        else:
            with self:
                self.execute("DELETE FROM embed_assets WHERE message_id IS NULL AND created_at<?", (cutoff,))
        self.cleanup_unused_files()

    def calculate_file_hash(self, file_path: str) -> str:
        """Calculate SHA256 hash of a file"""
        hash_sha256=hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_sha256.update(chunk)
            return hash_sha256.hexdigest()
        except IOError as e:
            logger.error(f"Failed to calculate hash for {file_path}: {e}")
            raise

