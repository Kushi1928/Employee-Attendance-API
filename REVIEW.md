# Starter code review

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It assumes the supplied datetime's date and timezone are the shift's local date and timezone; that is not guaranteed for UTC or overnight inputs. | Pass a UTC instant or use an early-morning overnight punch. | Normalize the instant to IST and apply the attendance-day/shift-start rules. |
| 2 | `compute_late_minutes` | It compares the floored whole-minute count to 10, incorrectly treating 10 minutes and 1 second late as on time. | Shift starts 09:30; punch at 09:40:01 should report 10 late minutes. | Compare elapsed seconds strictly to 600, then floor elapsed whole minutes. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even and binary floats; the contract requires decimal half-up. | A duration whose hour value is exactly halfway at two decimals. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It attaches IST to an end time based on the calendar date, which is wrong for overnight shifts. | Shift 22:00–06:00 and punch out at 06:40 next day. | Resolve the shift end from the attendance day and shift overnight flag. |
| 5 | `create_employee` | Duplicate detection is check-then-insert and can race; no database uniqueness was enforced. | Send two concurrent creates with the same `emp_code`. | Create a unique index at startup and translate `DuplicateKeyError` to 409. |
| 6 | `create_employee` | It returns BSON `created_at` as a datetime instead of epoch milliseconds. | Inspect the JSON response. | Convert instants at the API boundary. |
| 7 | `list_employees` | `skip(page * page_size)` makes page 1 start after the first page. | Request page 1 with 20 rows. | Use `(page - 1) * page_size`. |
| 8 | `list_employees` | `total` ignores the department filter and results have no required stable ordering. | Filter by a department or compare repeated paginated requests. | Count the filtered query and sort by `emp_code`. |
| 9 | `punch_in` | It does not return 404 for an unknown employee and then dereferences `None`. | Punch in with an unknown code. | Check the employee first and return 404. |
| 10 | `punch_in` | `datetime.fromtimestamp` uses the host timezone, values are not truncated, and the attendance date ignores overnight early punches. | Run under a non-IST host timezone or punch at 02:00 on an overnight shift. | Parse epoch milliseconds as UTC, truncate to seconds, derive the IST attendance date per R1. |
| 11 | `punch_in` | It does not validate presence status, does not initialize all derived fields consistently, and inserts without a concurrency guarantee. | Send `ABSENT`, or race two same-day punch-ins. | Validate status and defaults; enforce unique (`emp_code`, `date`) in MongoDB and handle duplicate-key conflict. |
| 12 | `list_attendance` | It loads and sorts the entire collection in Python, so it does not scale. | Use a large collection and inspect memory/query behavior. | Use indexed MongoDB filtering, sorting, count, skip, and limit. |
| 13 | `list_attendance` | It has no date range validation, no page bounds, returns an `id` derived from Mongo `_id`, and emits BSON datetimes rather than epoch milliseconds. | Reverse date bounds, use page 0, or inspect the response shape. | Validate parameters, apply R10 bounds, omit internal IDs, and serialize instants at the API boundary. |
| 14 | Helpers and endpoints | Naive `datetime.now()` values are stored as if UTC, making instants ambiguous. | Read a stored timestamp from MongoDB in a different timezone. | Use timezone-aware UTC datetimes for all persisted instants. |

## Checked and retained

The starter's basic collection names, use of `emp_code` for employee lookup, and the idea of storing derived attendance fields are consistent with the supplied data model. Punch-in and punch-out are also correctly absent from the manual correction history by design.
