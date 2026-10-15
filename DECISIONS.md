# DECISIONS.md

### 1. Indexes

I added indexes to make attendance searches and reports faster. Since an employee should have only one attendance record per day, I added a unique index on `emp_code` and `date`. This also prevents duplicate attendance records.

I added other indexes to make the common operations faster, like viewing attendance by date, filtering by status, and finding an employee's latest punch-in time. I also added a unique index on `emp_code` in the employees collection.

I didn't add a separate index on `date` because the existing date-and-employee index already helps with date-based searches.

### 2. Handling Duplicate Punch-ins

Two punch-in requests could arrive at almost the same time for the same employee. If both requests check for an existing record before either one inserts it, both might try to create a new record.

To avoid this, I rely on the unique index on `emp_code` and `date`. It allows only one record for that employee on that day. If another request tries to insert a duplicate, MongoDB raises a `DuplicateKeyError`, and the application returns a `409 Conflict` response.

This way, the database handles the race condition instead of relying only on an application-level check.

### 3. Handling Leaderboard Ties

If two employees have the same total late minutes, I needed a consistent way to decide their order.

So, I sort the leaderboard by total late minutes in descending order. If two employees have the same value, I sort them by `emp_code` in ascending order before applying the limit.

This makes the results predictable. For example, if two employees are tied for the last available position, the employee with the smaller `emp_code` comes first.

### 4. Department Headcount

For the department summary, I start with the employees collection and use `$lookup` to get their attendance records.

I chose this approach because I want to count all employees, not just those who have attendance records. If I started with attendance logs, employees without any logs could be left out of the summary.

By starting with employees, I can include everyone and show zero attendance-related values where no matching logs exist.

### 5. Handling 100x More Data

If the amount of attendance data increased significantly, I would improve the listing and reporting operations.

First, I would replace `skip` and `limit` pagination with cursor-based pagination. With very large datasets, skipping many records can become slow. Cursor-based pagination would let me continue from the last record I viewed.

I would also consider storing precomputed monthly attendance totals instead of calculating them from all the raw attendance records every time a report is requested.

These changes would help the application handle larger datasets more efficiently.