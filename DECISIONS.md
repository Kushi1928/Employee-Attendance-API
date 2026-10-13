# Design decisions

1. **Indexes.** `employees.emp_code` is unique. `(department, emp_code)` supports directory filters; `(department, joined_on)` and `(joined_on)` support headcount queries. Attendance `(emp_code, date)` enforces one record per day. Date/employee and status/date/employee support list queries; date/status supports monthly scans. I rejected a `late_minutes` index because the month date range is the better filter.

2. **Punch-in race.** Concurrent requests can both read the employee and compute the same day. The unique attendance key accepts one insert; MongoDB rejects the other, and the API returns 409. The initial read is not relied on for uniqueness.

3. **Ties.** `$rank` gives equal totals the same rank and skips the next rank. The limit filters by rank, so a tie at the cutoff is returned in full. Rows are ordered by total minutes, then employee code.

4. **Headcount.** The summary starts with employees who joined by month end, then looks up logs. It groups by employee before department, so a person without logs still adds one to headcount.

5. **100x scale.** I would measure explain plans and workload, then consider monthly rollups maintained from source records. That reduces repeated aggregation work but adds consistency and refresh costs.
