import copy
import unittest
from pathlib import Path
from unittest.mock import patch
import hhan_pzone_core as core
import hhan_pzone_manager as manager
import test_v6 as fixtures
from test_v6 import Session, Response, torrent, torrent_bytes, candidate, item, page

class V6RegressionTests(unittest.TestCase):
    setUp = fixtures.V6Tests.setUp
    tearDown = fixtures.V6Tests.tearDown

    def test_growth_checks_actual_completed_plus_reserved_quota(self):
        core.LAST_STRATEGY_CONTEXT = {'strategy_mode': 'GROW', 'completed_hashes': ['a'*40]}
        session = Session([torrent(size=90)])
        with patch.object(core, 'curl_download_torrent', return_value=torrent_bytes(20)):
            core.execute_actions(session, [candidate(size=5)], [])
        self.assertFalse(session.posts)

    def test_growth_includes_existing_download_reservations_in_quota(self):
        core.LAST_STRATEGY_CONTEXT = {'strategy_mode': 'GROW', 'completed_hashes': ['a'*40]}
        session = Session([torrent(size=70), torrent('b'*40, 20, 0.5)])
        with patch.object(core, 'curl_download_torrent', return_value=torrent_bytes(20)):
            core.execute_actions(session, [candidate(size=5)], [])
        self.assertFalse(session.posts)

    def test_repeated_declared_last_page_does_not_publish_partial(self):
        with patch.object(core, 'curl_get_hhan', side_effect=[page(1,1), page(1,1)]):
            with self.assertRaises(RuntimeError):
                core.fetch_remote_preservation_records()

    def test_empty_declared_last_page_does_not_publish_partial(self):
        with patch.object(core, 'curl_get_hhan', side_effect=[page(1,1), page(None,1)]):
            with self.assertRaises(RuntimeError):
                core.fetch_remote_preservation_records()

    def test_stale_plan_cannot_commit_strategy_reference(self):
        core.save_cache(core.empty_cache())
        before = Path(core.CACHE_PATH).read_bytes()
        core.LAST_STRATEGY_CONTEXT = {'completed_hashes': ['a'*40], 'strategy_state': {'reference_value':999, 'space_optimized':True}}
        core.execute_actions(Session([torrent('b'*40)]), [], [])
        self.assertEqual(Path(core.CACHE_PATH).read_bytes(), before)

    def test_category_change_during_download_aborts_deletions(self):
        class ChangingSession(Session):
            def get(self, url, params=None, **kwargs):
                if url.endswith('/info') and self.posts and not params:
                    return Response([torrent('b'*40,10)])
                return super().get(url, params=params, **kwargs)
        core.LAST_STRATEGY_CONTEXT = {'completed_hashes':['a'*40], 'value_floor':0}
        session = ChangingSession([torrent('a'*40,10)])
        with patch.object(core, 'curl_download_torrent', return_value=torrent_bytes(5)):
            core.execute_actions(session, [candidate(size=5)], [dict(item('a'*40,10),reason='SPACE')])
        self.assertFalse(any(url.endswith('/setCategory') for url,_ in session.posts))

    def test_newly_found_existing_hash_still_reserves_buffer(self):
        session = Session(add_text='Fails.', hash_exists=True)
        data1=torrent_bytes(30)
        data2=data1.replace(b'4:test', b'5:test2')
        with patch.object(core, 'curl_download_torrent', side_effect=[data1,data2]):
            core.execute_actions(session, [candidate('1',10),candidate('2',10)], [])
        self.assertEqual(sum(url.endswith('/add') for url,_ in session.posts),1)

    def test_category_http_failure_is_reported_as_failure(self):
        class RejectingSession(Session):
            def post(self,url,**kwargs):
                self.posts.append((url,kwargs))
                return Response(status=409)
        core.LAST_STRATEGY_CONTEXT = {'completed_hashes':['a'*40], 'value_floor':0}
        with self.assertRaises(RuntimeError):
            core.execute_actions(RejectingSession([torrent('a'*40,10)]), [], [dict(item('a'*40,10),reason='SPACE')])

if __name__ == '__main__':
    unittest.main()
