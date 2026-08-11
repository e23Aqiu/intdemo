from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrated_client.config import get_database_path


def main() -> None:
    database_path = get_database_path()
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        state = dict(
            connection.execute(
                "SELECT * FROM sync_state WHERE id=1"
            ).fetchone()
        )
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM sync_outbox
            WHERE server_account_id=?
            GROUP BY status
            ORDER BY status
            """,
            (state["current_server_account_id"],),
        ).fetchall()
        print(
            json.dumps(
                {
                    "database": str(database_path),
                    "sync_state": state,
                    "outbox": [dict(row) for row in rows],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
