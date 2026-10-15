# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). Punch-in/out with business rules, manual
corrections with an audit trail, and analytics built on aggregation pipelines.

## Run
1. Start MongoDB 6+ locally (or use a MongoDB Atlas URI)
2. `python -m venv .venv` and activate it
3. `pip install -r requirements.txt`
4. Optional `.env`: `MONGO_URI=mongodb://localhost:27017`, `MONGO_DB=attendance_db`
5. `uvicorn app.main:app --port 8000`, then open http://localhost:8000/docs

## What is implemented
- Employees: create (validated, unique emp_code), list with filter and pagination
- Punch-in / punch-out with late, overtime, work-hours and half-day rules (IST)
- Manual correction (PATCH) with audit history and compare-and-set concurrency
- Attendance listing with filters, sorting and pagination done inside MongoDB
- Analytics: employee monthly summary, late-minutes leaderboard, department summary
- Indexes created at startup; health check pings MongoDB

## Not done / known gaps
- (Fill this in honestly after you compare with the OpenAPI file.)

## Docs
- REVIEW.md: defects found in the starter code
- DECISIONS.md: design decisions