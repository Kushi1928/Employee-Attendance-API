"""Integration tests for the assignment API. Requires a reachable MongoDB 6+ instance."""
import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main

UTC = timezone.utc


def instant(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


def employee(code, department="Engineering", joined="2026-07-01", start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Employee {code}", "email": f"{code.lower()}@example.test",
            "department": department, "shift_start": start, "shift_end": end,
            "joined_on": joined, "created_at": datetime(2026, 1, 1, tzinfo=UTC)}


def log(code, day, status="PRESENT", late=0, overtime=0, hours=8.0, half=False, punch_in=None, punch_out=None):
    return {"emp_code": code, "date": day, "status": status,
            "punch_in": datetime.fromisoformat(punch_in).replace(tzinfo=UTC) if punch_in else None,
            "punch_out": datetime.fromisoformat(punch_out).replace(tzinfo=UTC) if punch_out else None,
            "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
            "half_day": half, "history": []}


class AttendanceApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        cls.mongo = MongoClient(uri, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=2500)
        try:
            cls.mongo.admin.command("ping")
            version = cls.mongo.admin.command("buildInfo")["versionArray"]
        except Exception as exc:
            cls.mongo.close()
            raise unittest.SkipTest(f"MongoDB is unavailable: {exc}")
        if version[0] < 6:
            raise unittest.SkipTest("MongoDB 6.0 or newer is required")
        cls.db_name = f"attendance_api_test_{uuid4().hex}"
        cls.db = cls.mongo[cls.db_name]
        # Reproduce an equivalent index carrying a legacy/custom name.
        cls.db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="attendance_date_code")
        main.client.close()  # Close the module's default client before swapping in the isolated test client.
        main.client, main.db = cls.mongo, cls.db
        cls.http = TestClient(main.app)
        cls.http.__enter__()
        main.ensure_indexes()  # Startup index creation must be idempotent.

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "http", None):
            cls.http.__exit__(None, None, None)
        if getattr(cls, "db_name", None):
            cls.mongo.drop_database(cls.db_name)
        if getattr(cls, "mongo", None):
            cls.mongo.close()

    def setUp(self):
        self.db.employees.delete_many({})
        self.db.attendance_logs.delete_many({})

    def test_health_and_employee_validation_and_pagination(self):
        self.assertEqual(self.http.get("/health").status_code, 200)
        body = {"emp_code": "EMP9001", "name": "Asha Rao", "email": "asha@example.test",
                "department": "Engineering", "joined_on": "2026-07-01"}
        created = self.http.post("/employees", json=body)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertIsInstance(created.json()["created_at"], int)
        self.assertEqual(self.http.post("/employees", json=body).status_code, 409)
        invalid = dict(body, emp_code="bad-code")
        self.assertEqual(self.http.post("/employees", json=invalid).status_code, 422)
        self.db.employees.insert_many([employee(f"EMP90{i:02d}") for i in range(2, 25)])
        page = self.http.get("/employees?page=1&page_size=20").json()
        self.assertEqual(len(page["items"]), 20)
        self.assertEqual(page["total"], 24)
        self.assertEqual(page["items"][0]["emp_code"], "EMP9001")

    def test_punch_rules_and_conflicts(self):
        self.db.employees.insert_one(employee("EMP9101"))
        # 09:40:00 IST is exactly the grace boundary; 09:40:01 is late by 10 minutes.
        exact = self.http.post("/attendance/punch-in", json={"emp_code": "EMP9101", "punched_at": instant("2026-10-13T04:10:00")})
        self.assertEqual(exact.status_code, 201, exact.text)
        self.assertEqual(exact.json()["late_minutes"], 0)
        self.assertEqual(exact.json()["date"], "2026-10-13")
        self.assertEqual(self.http.post("/attendance/punch-in", json={"emp_code": "EMP9101", "punched_at": instant("2026-10-13T04:10:01")}).status_code, 409)

        self.db.employees.insert_one(employee("EMP9102"))
        late = self.http.post("/attendance/punch-in", json={"emp_code": "EMP9102", "punched_at": instant("2026-10-13T04:10:01")})
        self.assertEqual(late.json()["late_minutes"], 10)
        out_ms = instant("2026-10-13T07:40:01")  # 3.5 hours after punch-in
        closed = self.http.post("/attendance/punch-out", json={"emp_code": "EMP9102", "punched_at": out_ms})
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["work_hours"], 3.5)
        self.assertTrue(closed.json()["half_day"])
        self.assertEqual(self.http.post("/attendance/punch-out", json={"emp_code": "EMP9102", "punched_at": out_ms}).status_code, 409)
        self.assertEqual(self.http.post("/attendance/punch-in", json={"emp_code": "EMP9102", "punched_at": "1783396800000"}).status_code, 422)

        # Half-day is based on rounded hours and uses a strict < 4.50 threshold.
        self.db.employees.insert_one(employee("EMP9103"))
        start_ms = instant("2026-10-13T04:00:00")
        self.http.post("/attendance/punch-in", json={"emp_code": "EMP9103", "punched_at": start_ms})
        boundary = self.http.post("/attendance/punch-out", json={"emp_code": "EMP9103", "punched_at": instant("2026-10-13T08:30:00")}).json()
        self.assertEqual(boundary["work_hours"], 4.5)
        self.assertFalse(boundary["half_day"])

    def test_overnight_attendance_day_and_overtime(self):
        self.db.employees.insert_one(employee("EMP9201", start="22:00", end="06:00"))
        punched = self.http.post("/attendance/punch-in", json={"emp_code": "EMP9201", "punched_at": instant("2026-10-12T19:00:00")})
        self.assertEqual(punched.status_code, 201, punched.text)
        self.assertEqual(punched.json()["date"], "2026-10-12")  # 00:30 IST on Oct 13 belongs to prior shift day.
        self.assertEqual(punched.json()["late_minutes"], 150)  # Compared with 22:00 on Oct 12, not Oct 13.
        closed = self.http.post("/attendance/punch-out", json={"emp_code": "EMP9201", "punched_at": instant("2026-10-13T01:00:00")})
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["overtime_minutes"], 30)
        self.assertEqual(closed.json()["work_hours"], 6.0)

    def test_regularization_audit_and_noop_rejection(self):
        self.db.employees.insert_one(employee("EMP9301"))
        self.db.attendance_logs.insert_one(log("EMP9301", "2026-07-07", late=35, hours=8.42,
                                               punch_in="2026-07-07T04:35:00", punch_out="2026-07-07T13:00:00"))
        response = self.http.patch("/attendance/EMP9301/2026-07-07", json={
            "punch_in": instant("2026-07-07T03:58:00"), "reason": "biometric glitch", "regularized_by": "hr.admin"})
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()
        self.assertEqual(record["late_minutes"], 0)
        self.assertEqual(record["work_hours"], 9.03)
        self.assertEqual(len(record["history"]), 1)
        self.assertEqual(record["history"][0]["changes"]["punch_in"]["from"], instant("2026-07-07T04:35:00"))
        self.assertEqual(record["history"][0]["changes"]["punch_in"]["to"], instant("2026-07-07T03:58:00"))
        self.assertEqual(self.http.patch("/attendance/EMP9301/2026-07-07", json={
            "reason": "biometric glitch", "regularized_by": "hr.admin"}).status_code, 422)

    def seed_analytics(self):
        self.db.employees.insert_many([
            employee("EMP9401", "Engineering", "2026-07-01"),
            employee("EMP9402", "Engineering", "2026-07-08"),  # no logs, still in headcount from July 8
            employee("EMP9403", "Sales", "2026-07-01"),
            employee("EMP9404", "Sales", "2026-07-01"),
            employee("EMP9405", "Engineering", "2026-07-01"),
        ])
        self.db.attendance_logs.insert_many([
            log("EMP9401", "2026-07-06", late=70, overtime=40, hours=9.08, punch_in="2026-07-06T04:35:00", punch_out="2026-07-06T13:40:00"),
            log("EMP9401", "2026-07-07", status="WFH", half=True, hours=3.5, punch_in="2026-07-07T04:00:00", punch_out="2026-07-07T07:30:00"),
            log("EMP9401", "2026-07-08", status="LEAVE", hours=None),
            log("EMP9401", "2026-07-11", late=50, hours=8.0, punch_in="2026-07-11T04:35:00", punch_out="2026-07-11T12:35:00"),  # weekend: late, not present-day count
            log("EMP9403", "2026-07-06", late=90, hours=8.0, punch_in="2026-07-06T04:35:00", punch_out="2026-07-06T12:35:00"),
            log("EMP9404", "2026-07-06", late=90, hours=8.0, punch_in="2026-07-06T04:35:00", punch_out="2026-07-06T12:35:00"),
            log("EMP9405", "2026-07-07", late=40, hours=8.0, punch_in="2026-07-07T04:35:00", punch_out="2026-07-07T12:35:00"),
        ])

    def test_monthly_department_leaderboard_ties_and_trend_gaps(self):
        self.seed_analytics()
        monthly = self.http.get("/analytics/employees/EMP9401/monthly?month=2026-07")
        self.assertEqual(monthly.status_code, 200, monthly.text)
        self.assertEqual(monthly.json()["working_days"], 23)
        self.assertEqual(monthly.json()["present_days"], 1.5)
        self.assertEqual(monthly.json()["late_count"], 2)
        self.assertEqual(monthly.json()["total_late_minutes"], 120)

        summary = self.http.get("/analytics/departments/summary?month=2026-07&department=Engineering").json()["items"][0]
        self.assertEqual(summary["headcount"], 3)  # Includes EMP9402 despite no attendance documents.
        self.assertEqual(summary["present_days"], 2.5)
        self.assertEqual(summary["leave_count"], 1)

        board = self.http.get("/analytics/leaderboard/late?month=2026-07&limit=2").json()["items"]
        self.assertEqual([(x["rank"], x["emp_code"]) for x in board], [(1, "EMP9401"), (2, "EMP9403"), (2, "EMP9404")])

        trend_response = self.http.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
        self.assertEqual(trend_response.status_code, 200, trend_response.text)
        trend = trend_response.json()["items"]
        self.assertEqual(len(trend), 7)
        self.assertEqual(trend[0]["date"], "2026-07-06")
        self.assertEqual(trend[0]["attendance_rate"], 0.5)
        self.assertEqual(trend[1]["attendance_rate"], 0.75)
        self.assertEqual(trend[2]["headcount"], 3)
        self.assertEqual(trend[2]["attendance_rate"], 0.0)
        self.assertEqual(trend[2]["moving_avg_7d"], 0.4167)
        self.assertFalse(trend[5]["is_working_day"])
        self.assertIsNone(trend[5]["attendance_rate"])

    def test_explain_endpoints_use_index_scans(self):
        self.seed_analytics()
        requests = [
            "/admin/explain/attendance_list?page=1&page_size=5",
            "/admin/explain/employee_monthly?emp_code=EMP9401&month=2026-07",
            "/admin/explain/department_summary?month=2026-07",
            "/admin/explain/late_leaderboard?month=2026-07&limit=2",
            "/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-12",
        ]
        for path in requests:
            with self.subTest(path=path):
                response = self.http.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                payload = str(response.json()["explain"])
                self.assertIn("IXSCAN", payload)
                self.assertNotIn("COLLSCAN", payload)


if __name__ == "__main__":
    unittest.main()
