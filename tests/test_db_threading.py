"""The control plane's one SQLite connection, shared by request threads and job threads.

create_instance returned 404 'no such instance' for the instance it had just created, once in a
while, when its deploy job (a ThreadRunner thread) ran at the same moment: Database.one() fetched
its row OUTSIDE the connection lock, so the fetch interleaved with the job thread's statements on the
same connection and came back empty. This hammers exactly that shape (two threads reading the same
row by the same SQL text while others update it and run transactions) and requires every read to
see the row.
"""
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from sirosid_service.db import Database  # noqa: E402
except ImportError as e:                       # cryptography missing: the service tests skip too
    Database = None
    _WHY = str(e)

SELECT = "SELECT * FROM instances WHERE id=?"


@unittest.skipIf(Database is None, "needs the service's requirements")
class DatabaseThreadingTests(unittest.TestCase):
    def _hammer(self, db, seconds=1.0):
        db.execute("INSERT INTO instances(id,owner,name,status,naming,spec,config,created_at,updated_at) "
                    "VALUES('x','u','','creating','{}',x'00',x'00',0,0)")
        stop = threading.Event()
        misses, errors, reads = [], [], [0]

        def request():                      # get_instance -> _instance_row
            while not stop.is_set():
                try:
                    r = db.one(SELECT, ("x",))
                    reads[0] += 1
                    if r is None or r["owner"] != "u":
                        misses.append(r)
                    db.all("SELECT * FROM instances WHERE owner=?", ("u",))
                except Exception as e:      # noqa: BLE001
                    errors.append(repr(e))

        def job():                          # _deploy_job: read the row, save state, set status
            while not stop.is_set():
                try:
                    db.one(SELECT, ("x",))
                    with db.transaction() as d:
                        d.execute("DELETE FROM state WHERE instance_id=?", ("x",))
                        d.execute("INSERT INTO state(instance_id,path,data) VALUES(?,?,?)", ("x", "p", b"1"))
                    db.execute("UPDATE instances SET status=?, updated_at=? WHERE id=?", ("running", time.time(), "x"))
                except Exception as e:      # noqa: BLE001
                    errors.append(repr(e))

        threads = [threading.Thread(target=f) for f in (request, request, job, job)]
        for t in threads:
            t.start()
        time.sleep(seconds)
        stop.set()
        for t in threads:
            t.join()
        return reads[0], misses, errors

    def test_concurrent_reads_never_miss_a_committed_row(self):
        db = Database(":memory:")
        try:
            reads, misses, errors = self._hammer(db)
        finally:
            db.close()
        self.assertGreater(reads, 100)
        self.assertEqual(errors, [], errors[:3])
        self.assertEqual(len(misses), 0, f"{len(misses)} of {reads} reads missed the row")


if __name__ == "__main__":
    unittest.main()
