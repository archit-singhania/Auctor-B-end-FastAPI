import unittest
from app.domain import hash_password,check_password,score_from,grade,CATALOG,public_question
class DomainTests(unittest.TestCase):
    def test_password_hashes_salted_and_fail_closed(self):
        a=hash_password('long-enough-password'); b=hash_password('long-enough-password')
        self.assertNotEqual(a,b); self.assertTrue(check_password('long-enough-password',a)); self.assertFalse(check_password('wrong',a)); self.assertFalse(check_password('x','broken'))
    def test_scores_require_verified_evidence_and_stable_badges(self):
        e=[{'kind':'github','status':'pending','detail':{}},{'kind':'project','status':'verified','detail':{'project':'API'}}]
        s=score_from(e,['jwt-auth','jwt-auth'],[{'name':'API'},{'name':'Site'}])
        self.assertEqual(s['total'],1.35); self.assertEqual(s['components']['github']['fraction'],0)
    def test_max_score_and_invalid_weights(self):
        e=[{'kind':k,'status':'verified','detail':{'solved':300,'project':'API'}} for k in ['github','coding','project','experience']]
        self.assertEqual(score_from(e,list(CATALOG),[{'name':'API'}])['total'],10)
        with self.assertRaises(ValueError): score_from([],[],[],{'github':1,'leetcode':1,'badges':0,'projects':0,'experience':0})
    def test_answers_never_in_public_question_and_server_grades(self):
        for key,(_,_,questions) in CATALOG.items():
            ids=list(range(5)); answers={str(i):q[2] for i,q in enumerate(questions)}
            self.assertEqual(grade(key,ids,answers),(5,True))
            self.assertNotIn('correct',public_question(key,0))
            with self.assertRaises(ValueError): grade(key,ids,{'0':0})
            with self.assertRaises(ValueError): grade(key,ids,{str(i):99 for i in ids})
if __name__=='__main__': unittest.main()
