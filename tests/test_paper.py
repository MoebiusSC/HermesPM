import os, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
import app

class PaperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB = str(Path(self.tmp.name) / 'test.db')
        app.init()
        self.now = int(time.time())
        self.address = '0x' + 'a'*40
        self.trade = {'transactionHash':'0xabc','side':'BUY','asset':'123','timestamp':self.now-10,'price':.40,'size':30,'title':'Test market','outcome':'Yes'}
        self.prior = app.book_price
    def tearDown(self):
        app.book_price = self.prior
        self.tmp.cleanup()
    def test_forward_only_and_idempotent(self):
        app.book_price = lambda asset,side: (.42, 100)
        with app.connect() as db:
            app.process_trade(db,self.address,self.trade,self.now,True)
            app.process_trade(db,self.address,self.trade,self.now,True)
            self.assertEqual(db.execute('SELECT count(*) FROM fills').fetchone()[0],1)
            self.assertAlmostEqual(float(db.execute("SELECT value FROM settings WHERE key='cash'").fetchone()[0]),987.4)
    def test_stale_signal_never_fills(self):
        self.trade['timestamp']=self.now-600
        app.book_price=lambda *args: self.fail('book request must not run')
        with app.connect() as db:
            app.process_trade(db,self.address,self.trade,self.now,True)
            self.assertEqual(db.execute('SELECT count(*) FROM fills').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT state FROM signals').fetchone()[0],'skipped')
    def test_price_movement_blocks_copy(self):
        app.book_price=lambda *args:(.46,100)
        with app.connect() as db:
            app.process_trade(db,self.address,self.trade,self.now,True)
            self.assertEqual(db.execute('SELECT count(*) FROM fills').fetchone()[0],0)

if __name__=='__main__': unittest.main()
