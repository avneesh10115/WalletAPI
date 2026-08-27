import argparse
import json
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from werkzeug.serving import WSGIRequestHandler, make_server

from app import create_app


class SilentHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        pass


def post_json(url, data, headers=None):
    body = json.dumps(data).encode()
    request = Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)

    with urlopen(request, timeout=15) as response:
        return response.status, json.loads(response.read())


def get_json(url):
    with urlopen(url, timeout=15) as response:
        return json.loads(response.read())


def percentile(values, percent):
    position = int((percent / 100) * len(values))
    position = min(max(position, 1), len(values))
    return sorted(values)[position - 1]


def run_load_test(request_count, workers):
    with tempfile.TemporaryDirectory() as folder:
        database = f"{folder}/load_test.db"
        app = create_app(database)
        server = make_server(
            "127.0.0.1", 0, app, threaded=True, request_handler=SilentHandler
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        try:
            starting_balance = request_count + 1000
            _, sender = post_json(
                f"{base_url}/accounts",
                {"owner": "Load Test Sender", "initial_balance": starting_balance},
            )
            _, receiver = post_json(
                f"{base_url}/accounts",
                {"owner": "Load Test Receiver", "initial_balance": 0},
            )

            def send_transfer(number):
                data = {
                    "from_account": sender["id"],
                    "to_account": receiver["id"],
                    "amount": 1,
                }
                start = time.perf_counter()
                try:
                    status, _ = post_json(
                        f"{base_url}/transfers",
                        data,
                        {"Idempotency-Key": f"load-test-{number}"},
                    )
                    return status, time.perf_counter() - start
                except (HTTPError, URLError, TimeoutError):
                    return 0, time.perf_counter() - start

            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(send_transfer, range(request_count)))
            total_time = time.perf_counter() - start

            successful = sum(result[0] == 201 for result in results)
            failed = request_count - successful
            latencies = [result[1] * 1000 for result in results]

            sender_after = get_json(f"{base_url}/accounts/{sender['id']}")
            receiver_after = get_json(f"{base_url}/accounts/{receiver['id']}")
            db = sqlite3.connect(database)
            saved_transfers = db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0]
            db.close()

            balances_ok = (
                sender_after["balance"] + receiver_after["balance"]
                == starting_balance
            )
            rows_ok = saved_transfers == successful

            print(f"Requests:       {request_count}")
            print(f"Workers:        {workers}")
            print(f"Successful:     {successful}")
            print(f"Failed:         {failed}")
            print(f"Total time:     {total_time:.2f} s")
            print(f"Throughput:     {successful / total_time:.2f} requests/s")
            print(f"p50 latency:    {percentile(latencies, 50):.2f} ms")
            print(f"p95 latency:    {percentile(latencies, 95):.2f} ms")
            print(f"p99 latency:    {percentile(latencies, 99):.2f} ms")
            print(f"Balance check:  {'passed' if balances_ok else 'failed'}")
            print(f"Database check: {'passed' if rows_ok else 'failed'}")

            if failed or not balances_ok or not rows_ok:
                raise SystemExit(1)
        finally:
            server.shutdown()
            server_thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send transfers to the Wallet API")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()

    if args.requests < 1 or args.workers < 1:
        parser.error("requests and workers must be positive")

    run_load_test(args.requests, args.workers)