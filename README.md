# Employee Attendance & Analytics API

This is my submission for the HROne Software Engineer Trainee assignment. It is built with FastAPI and MongoDB, and all the code is in app/main.py.

## How to run

Install the packages with `pip install -r requirements.txt`, then create a .env file with MONGO_URI and MONGO_DB (real environment variables take priority over the file). Start the app with `uvicorn app.main:app --port 8000` and open /docs to try it. The indexes are created automatically when the app starts.

## What is done

Health check, creating and listing employees, punch-in, punch-out (including overnight shifts), the attendance list, manual corrections with an audit trail, and the monthly summary for one employee. Punch-in, punch-out and corrections are safe against simultaneous requests, using a unique index and conditional updates.

## What is missing

Right now the department summary, the late leaderboard, the department trend and the explain endpoint are not finished. I will update this file if that changes before the deadline.