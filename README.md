# Wallet API

This is a small REST API made with Python, Flask and SQLite. It creates accounts
and transfers money between them. There is no frontend.

I made this project to understand concurrent requests, SQLite transactions and
idempotency. It does not have authentication or authorization.

## Stack

- Python and Flask
- SQLite using Python's `sqlite3` module

Money is stored as an integer. For example, 100 means 100 paise or 100 cents.
This avoids rounding errors from floating-point numbers.

## Run locally

Create a virtual environment and install the two packages:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

The API starts at `http://127.0.0.1:5000`.

## Using the API

Create two accounts:

```powershell
curl.exe -X POST http://127.0.0.1:5000/accounts `
  -H "Content-Type: application/json" `
  -d '{"owner":"Alice","initial_balance":1000}'

curl.exe -X POST http://127.0.0.1:5000/accounts `
  -H "Content-Type: application/json" `
  -d '{"owner":"Bob","initial_balance":0}'
```

Transfer 250 units from account 1 to account 2:

```powershell
curl.exe -X POST http://127.0.0.1:5000/transfers `
  -H "Content-Type: application/json" `
  -H "Idempotency-Key: payment-001" `
  -d '{"from_account":1,"to_account":2,"amount":250}'
```

Read an account or transfer:

```powershell
curl.exe http://127.0.0.1:5000/accounts/1
curl.exe http://127.0.0.1:5000/transfers/1
```

If the same transfer is sent again with the same `Idempotency-Key`, the API
returns the first result. It also adds `Idempotent-Replayed: true` to the response
headers. If the data is changed but the same key is used, the API returns
`409 Conflict`.

## How a transfer works

Each transfer runs inside one `BEGIN IMMEDIATE` transaction:

1. Check if the idempotency key was used before.
2. Check if both accounts exist.
3. Subtract money from the sender only if there is enough balance.
4. Add the same amount to the receiver and save the transfer.
5. Commit everything together. If anything fails, roll back the transaction.

The `idempotency_key` column is unique. A hash of the request data is also saved.
This lets the API tell the difference between a repeated request and a different
request that reused the same key.

SQLite allows one writer at a time. `BEGIN IMMEDIATE` takes the write lock before
the API checks the idempotency key and balances. WAL mode allows other requests
to read while a write is running.

## Load test

```powershell
.\.venv\Scripts\python.exe load_test.py --requests 1000 --workers 10
```

The script starts the API on a temporary local port and sends transfers over
HTTP. It prints the number of successful and failed requests, throughput, and
p50, p95 and p99 latency. It also checks that:

- no account becomes negative
- total money remains unchanged
- the number of saved transfers matches the number of successful requests

Result on my laptop for 1,000 requests and 10 workers:

```text
Successful:     1000
Failed:         0
Total time:     14.87 s
Throughput:     67.24 requests/s
p50 latency:    108.86 ms
p95 latency:    366.36 ms
p99 latency:    780.02 ms
Balance check:  passed
Database check: passed
```

The result can change depending on the machine. SQLite allows one writer at a
time, so adding more workers does not always increase throughput.