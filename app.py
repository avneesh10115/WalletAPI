import hashlib
import json
import sqlite3
from pathlib import Path

from flask import Flask, g, jsonify, request


class ApiError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def create_app(database_path=None):
    app = Flask(__name__)
    app.config["DATABASE"] = str(
        database_path or Path(__file__).with_name("wallet.db")
    )

    def get_db():
        # Flask keeps this connection only for the current request.
        if "db" not in g:
            db = sqlite3.connect(app.config["DATABASE"], timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 10000")
            g.db = db
        return g.db

    def init_db():
        db = sqlite3.connect(app.config["DATABASE"])
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL CHECK (length(owner) BETWEEN 1 AND 100),
                balance INTEGER NOT NULL CHECK (balance >= 0),
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_account INTEGER NOT NULL REFERENCES accounts(id),
                to_account INTEGER NOT NULL REFERENCES accounts(id),
                amount INTEGER NOT NULL CHECK (amount > 0),
                idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                CHECK (from_account <> to_account)
            );
            """
        )
        db.close()

    def read_json(required_fields):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("Request body must be a JSON object", 400)

        missing = required_fields - data.keys()
        extra = data.keys() - required_fields
        if missing:
            raise ApiError(f"Missing fields: {', '.join(sorted(missing))}", 400)
        if extra:
            raise ApiError(f"Unexpected fields: {', '.join(sorted(extra))}", 400)
        return data

    def check_integer(value, field, minimum):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            message = f"{field} must be an integer greater than or equal to {minimum}"
            raise ApiError(message, 400)

    def account_to_dict(account):
        return {
            "id": account["id"],
            "owner": account["owner"],
            "balance": account["balance"],
            "created_at": account["created_at"],
        }

    def transfer_to_dict(transfer):
        return {
            "id": transfer["id"],
            "from_account": transfer["from_account"],
            "to_account": transfer["to_account"],
            "amount": transfer["amount"],
            "created_at": transfer["created_at"],
        }

    @app.teardown_appcontext
    def close_db(error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.errorhandler(ApiError)
    def handle_api_error(error):
        return jsonify({"error": error.message}), error.status_code

    @app.errorhandler(404)
    def handle_not_found(error):
        return jsonify({"error": "Endpoint not found"}), 404

    @app.post("/accounts")
    def create_account():
        data = read_json({"owner", "initial_balance"})
        owner = data["owner"]
        if not isinstance(owner, str) or not owner.strip() or len(owner.strip()) > 100:
            raise ApiError("owner must contain between 1 and 100 characters", 400)
        check_integer(data["initial_balance"], "initial_balance", 0)

        db = get_db()
        cursor = db.execute(
            "INSERT INTO accounts (owner, balance) VALUES (?, ?)",
            (owner.strip(), data["initial_balance"]),
        )
        db.commit()
        account = db.execute(
            "SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return jsonify(account_to_dict(account)), 201

    @app.get("/accounts/<int:account_id>")
    def get_account(account_id):
        account = get_db().execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account is None:
            raise ApiError("Account not found", 404)
        return jsonify(account_to_dict(account))

    @app.post("/transfers")
    def create_transfer():
        data = read_json({"from_account", "to_account", "amount"})
        check_integer(data["from_account"], "from_account", 1)
        check_integer(data["to_account"], "to_account", 1)
        check_integer(data["amount"], "amount", 1)
        if data["from_account"] == data["to_account"]:
            raise ApiError("Source and destination accounts must be different", 400)

        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not 1 <= len(idempotency_key) <= 128:
            raise ApiError("Idempotency-Key header must contain 1 to 128 characters", 400)

        sorted_data = json.dumps(data, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(sorted_data.encode()).hexdigest()
        db = get_db()

        # Take the write lock before checking the key. Two copies of the same
        # request cannot pass this check at the same time.
        db.execute("BEGIN IMMEDIATE")

        try:
            old_transfer = db.execute(
                "SELECT * FROM transfers WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if old_transfer is not None:
                if old_transfer["request_hash"] != request_hash:
                    raise ApiError(
                        "Idempotency key was already used with a different request", 409
                    )
                db.commit()
                headers = {"Idempotent-Replayed": "true"}
                return jsonify(transfer_to_dict(old_transfer)), 200, headers

            sender = db.execute(
                "SELECT id FROM accounts WHERE id = ?", (data["from_account"],)
            ).fetchone()
            receiver = db.execute(
                "SELECT id FROM accounts WHERE id = ?", (data["to_account"],)
            ).fetchone()
            if sender is None or receiver is None:
                raise ApiError("Source or destination account not found", 404)

            # Checking the balance in the UPDATE stops another request from
            # spending money that is no longer there.
            debit = db.execute(
                """
                UPDATE accounts
                SET balance = balance - ?
                WHERE id = ? AND balance >= ?
                """,
                (data["amount"], data["from_account"], data["amount"]),
            )
            if debit.rowcount != 1:
                raise ApiError("Insufficient balance", 409)

            db.execute(
                "UPDATE accounts SET balance = balance + ? WHERE id = ?",
                (data["amount"], data["to_account"]),
            )
            cursor = db.execute(
                """
                INSERT INTO transfers
                    (from_account, to_account, amount, idempotency_key, request_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    data["from_account"],
                    data["to_account"],
                    data["amount"],
                    idempotency_key,
                    request_hash,
                ),
            )
            transfer = db.execute(
                "SELECT * FROM transfers WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            db.commit()
            return jsonify(transfer_to_dict(transfer)), 201
        except Exception:
            db.rollback()
            raise

    @app.get("/transfers/<int:transfer_id>")
    def get_transfer(transfer_id):
        transfer = get_db().execute(
            "SELECT * FROM transfers WHERE id = ?", (transfer_id,)
        ).fetchone()
        if transfer is None:
            raise ApiError("Transfer not found", 404)
        return jsonify(transfer_to_dict(transfer))

    init_db()
    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)