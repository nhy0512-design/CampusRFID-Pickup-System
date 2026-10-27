"""Isolated database/API tests; no physical reader is required or accessed."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import app as service
import database as db
from rfid_reader import build_frame, find_serial_port, parse_inventory, crc16_ccitt


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / 'pickup.db')
        db.init_db()
        service.latest_card_event = None
        service.reader_fresh_connection = True
        service.awaiting_bind.clear()
        service.pending_unknown.clear()
        self.client = service.app.test_client()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def student(self, epc='DEMO001'):
        return db.add_student('DEMO001', '演示同学', '13800000001', epc)

    def scan(self, seconds, continuous=False):
        with patch.object(service.time, 'time', return_value=seconds):
            service.handle_rfid_event({'type': 'epc', 'epc': 'DEMO001', 'continuous': continuous})

    def test_new_database_is_empty(self):
        self.assertEqual(self.client.get('/api/students').json, [])
        self.assertEqual(self.client.get('/api/packages').json, [])

    def test_first_scan_shows_second_scan_checks_out(self):
        sid = self.student()
        db.add_package(None, 'DEMO-A', sid)
        db.add_package(None, 'DEMO-B', sid)
        self.scan(1000)
        self.assertEqual(service.latest_card_event['kind'], 'show')
        self.assertEqual(len(db.pending_for_student(sid)), 2)
        self.scan(1001, continuous=True)
        self.assertEqual(len(db.pending_for_student(sid)), 2)
        self.scan(1004)
        self.assertEqual(service.latest_card_event['picked_count'], 2)
        self.assertEqual(db.pending_for_student(sid), [])

    def test_expired_confirmation_does_not_check_out(self):
        sid = self.student(); db.add_package(None, 'DEMO-A', sid)
        self.scan(1000); self.scan(1304)
        self.assertEqual(service.latest_card_event['kind'], 'show')
        self.assertEqual(len(db.pending_for_student(sid)), 1)

    def test_binding_updates_latest_event(self):
        sid = self.student(epc=None)
        response = self.client.post('/api/bind-new-card', json={'student_id':sid,'epc':'DEMO001'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(service.latest_card_event['kind'],'show')
        self.assertEqual(db.get_student_by_id(sid)['rfid_epc'],'DEMO001')

    def test_cancel_binding_disarms_backend(self):
        sid=self.student(epc=None)
        route=f'/api/students/{sid}/await-bind'
        self.assertEqual(self.client.post(route).status_code,200)
        self.assertIn(sid, service.awaiting_bind)
        self.assertEqual(self.client.delete(route).status_code,200)
        self.assertNotIn(sid,service.awaiting_bind)

    def test_import_csv_preview_then_commit(self):
        raw='姓名,手机号\n演示同学,13800000001\n'.encode('utf-8-sig')
        r=self.client.post('/api/import/preview',data={'file':(io.BytesIO(raw),'students.csv'),'kind':'students'})
        self.assertEqual(r.status_code,200)
        r=self.client.post('/api/import/commit',json={'token':r.json['token']})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['created_students'],1)

    def test_debug_disabled_and_private_files_not_served(self):
        with patch.dict(os.environ, {'RFID_ENABLE_SIMULATION':'0'}):
            self.assertEqual(self.client.post('/api/debug/simulate',json={'epc':'DEMO001'}).status_code,403)
        self.assertEqual(self.client.get('/data/pickup.db').status_code,404)
        self.assertEqual(self.client.get('/backend/app.py').status_code,404)
        self.assertEqual(self.client.get('/').status_code,200)

    def test_known_protocol_frames(self):
        self.assertEqual(build_frame(0x01).hex(),'040001db4b')
        raw=bytes.fromhex('13000101010c300833b2ddd9014000000000')
        frame=raw+crc16_ccitt(raw).to_bytes(2,'little')
        self.assertEqual(parse_inventory(frame),['300833B2DDD9014000000000'])

    def test_explicit_port(self):
        with patch.dict(os.environ,{'RFID_PORT':'COM7'}):
            self.assertEqual(find_serial_port(),'COM7')


if __name__ == '__main__':
    unittest.main()
