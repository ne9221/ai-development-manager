import unittest
from manager.actions import ActionItem, STATUS_OPEN, STATUS_RESOLVED
from manager.tray_actions import actionable_snapshot
class TrayTests(unittest.TestCase):
 def test_truthful_actionable_snapshot(self):
  result=actionable_snapshot([ActionItem(action_id='old',title='old',status=STATUS_RESOLVED),ActionItem(action_id='new',title='new',severity='high',created_at='t',status=STATUS_OPEN)])
  self.assertEqual(result['count'],1); self.assertEqual(result['highest_severity'],'high'); self.assertEqual(result['actions'][0]['id'],'new')
if __name__=='__main__': unittest.main()
