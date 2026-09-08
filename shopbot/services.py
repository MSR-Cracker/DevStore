import json, uuid, re, logging
from datetime import datetime
from .database import Database

log=logging.getLogger(__name__)

class ShopService:
    REFERRAL_REWARD = 1
    USER_STARTING_CREDITS = 0
    ADMIN_STARTING_CREDITS = 10000
    def __init__(self, db: Database): self.db = db
    def setting(self, key, default=""):
        with self.db.connect() as c:
            row=c.execute("SELECT value FROM app_settings WHERE key=?",(key,)).fetchone()
        return row[0] if row else default
    def set_setting(self, key, value):
        with self.db.transaction() as c:c.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(key,value))
    def ensure_user(self, user, referral_code: str | None = None):
        if getattr(user,'is_bot',False): return False
        is_admin=user.id in (1905862979, 5943392316)
        if is_admin: referral_code = None
        with self.db.transaction() as c:
            existing = c.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (user.id,)).fetchone()
            if existing:
                c.execute("UPDATE users SET username=?,full_name=?,last_activity_at=CURRENT_TIMESTAMP WHERE telegram_id=?", (user.username, user.full_name, user.id))
                if is_admin and not c.execute("SELECT 1 FROM credit_transactions WHERE user_id=? AND kind='starting_credits'",(user.id,)).fetchone():
                    self.credit(c,user.id,self.ADMIN_STARTING_CREDITS,"starting_credits","registration",str(user.id),None)
                return
            referrer = int(referral_code) if referral_code and referral_code.isdigit() and int(referral_code) != user.id else None
            valid = referrer and c.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (referrer,)).fetchone()
            starting_credits=self.ADMIN_STARTING_CREDITS if is_admin else self.USER_STARTING_CREDITS
            c.execute("INSERT INTO users(telegram_id,username,full_name,credits,referral_owner_id) VALUES(?,?,?,?,?)", (user.id,user.username,user.full_name,starting_credits,referrer if valid else None))
            self.history(c, user.id, "registration", "تم تسجيل الحساب")
            if starting_credits:
                c.execute("INSERT INTO credit_transactions(user_id,amount,kind,balance_after,reference_type,reference_id,actor_id) VALUES(?,?,?,?,?,?,?)",(user.id,starting_credits,"starting_credits",starting_credits,"registration",str(user.id),None))
                self.history(c,user.id,"starting_credits",f"تمت إضافة رصيد البداية: {starting_credits} Credits")
            # الإحالة تُحفظ كمالك فقط — المكافأة تُصرف لاحقاً بعد الاشتراك الإجباري (try_reward_referral)
        return True
    def referral_rewards(self):
        """مكافآت الإحالة من إعدادات الأدمن: (مكافأة المُحيل, مكافأة المنضم)."""
        try: giver=float(self.setting("referral_giver_reward","1"))
        except ValueError: giver=1.0
        try: joiner=float(self.setting("referral_joiner_reward","0"))
        except ValueError: joiner=0.0
        return max(0,giver), max(0,joiner)
    def try_reward_referral(self, c, user_id):
        """يصرف مكافأتي الإحالة (مُحيل + منضم) بعد تحقق الاشتراكات. يرجع (referrer,giver,joiner) أو None."""
        u=c.execute("SELECT referral_owner_id FROM users WHERE telegram_id=?",(user_id,)).fetchone()
        if not u or not u['referral_owner_id']: return None
        referrer=u['referral_owner_id']
        if referrer == user_id: return None
        if c.execute("SELECT 1 FROM referrals WHERE referred_id=?",(user_id,)).fetchone(): return None
        if not c.execute("SELECT 1 FROM users WHERE telegram_id=?",(referrer,)).fetchone(): return None
        giver,joiner=self.referral_rewards()
        c.execute("INSERT INTO referrals(referrer_id,referred_id,reward) VALUES(?,?,?)",(referrer,user_id,giver))
        if giver > 0:
            self.credit(c, referrer, giver, "referral", "referral", str(user_id), None)
            c.execute("UPDATE users SET referral_earned=referral_earned+? WHERE telegram_id=?",(giver,referrer))
        if joiner > 0:
            self.credit(c, user_id, joiner, "referral_bonus", "referral", str(referrer), None)
        return referrer,giver,joiner
    def history(self, c, user_id, event, message, metadata=None):
        c.execute("INSERT INTO history(user_id,event_type,message,metadata) VALUES(?,?,?,?)", (user_id,event,message,json.dumps(metadata) if metadata else None))
    def credit(self,c,user_id,amount,kind,ref_type=None,ref_id=None,actor=None):
        row=c.execute("SELECT credits FROM users WHERE telegram_id=?",(user_id,)).fetchone()
        if not row or row[0]+amount < 0: raise ValueError("insufficient credits")
        balance=row[0]+amount
        c.execute("UPDATE users SET credits=? WHERE telegram_id=?",(balance,user_id))
        c.execute("INSERT INTO credit_transactions(user_id,amount,kind,balance_after,reference_type,reference_id,actor_id) VALUES(?,?,?,?,?,?,?)",(user_id,amount,kind,balance,ref_type,ref_id,actor))
        self.history(c,user_id,kind,f"تغيير الرصيد: {amount:+} Credits",{"balance":balance,"actor":actor})
        return balance
    def all_products(self,page):
        with self.db.connect() as c:
            total=c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            rows=c.execute("SELECT * FROM products ORDER BY id DESC LIMIT 10 OFFSET ?",( (page-1)*10, )).fetchall()
        return rows,total
    def all_active_product_ids(self):
        with self.db.connect() as c:
            rows=c.execute("SELECT id FROM products WHERE is_active=1").fetchall()
        return [r[0] for r in rows]
    def active_products_by_ids(self,pids):
        if not pids: return []
        with self.db.connect() as c:
            placeholders=','.join('?'*len(pids))
            rows=c.execute(f"SELECT * FROM products WHERE is_active=1 AND id IN ({placeholders})",tuple(pids)).fetchall()
        by_id={r['id']:r for r in rows}
        return [by_id[pid] for pid in pids if pid in by_id]
    def products(self,page,product_type='paid'):
        with self.db.connect() as c:
            total=c.execute("SELECT COUNT(*) FROM products WHERE is_active=1 AND product_type=?",(product_type,)).fetchone()[0]
            rows=c.execute("SELECT * FROM products WHERE is_active=1 AND product_type=? ORDER BY id DESC LIMIT 10 OFFSET ?",(product_type,(page-1)*10)).fetchall()
        return rows,total
    def product(self,pid):
        with self.db.connect() as c:return c.execute("SELECT * FROM products WHERE id=? AND is_active=1",(pid,)).fetchone()
    def purchase(self,user_id,pid):
        with self.db.transaction() as c:
            p=c.execute("SELECT * FROM products WHERE id=? AND is_active=1 AND (stock_quantity IS NULL OR stock_quantity > 0)",(pid,)).fetchone()
            if not p: raise ValueError("المنتج غير متاح")
            if not p["storage_path"]: raise ValueError("ملف المنتج غير جاهز")
            if p['product_type'] == 'paid':
                self.credit(c,user_id,-p["price"],"purchase","purchase",None,None)
            if p["stock_quantity"] is not None:
                c.execute("UPDATE products SET stock_quantity=stock_quantity-1 WHERE id=?",(pid,))
            price=0 if p['product_type'] == 'free' else p['price']
            cur=c.execute("INSERT INTO purchases(user_id,product_id,price,status) VALUES(?,?,?,'Paid')",(user_id,pid,price))
            purchase_id=cur.lastrowid; self.history(c,user_id,"free_product" if p['product_type']=='free' else "purchase",f"تم استلام {p['title']} مجاناً" if p['product_type']=='free' else f"تم شراء {p['title']}",{"purchase_id":purchase_id})
            return purchase_id,p
    def refund(self,purchase_id,reason):
        with self.db.transaction() as c:
            row=c.execute("SELECT * FROM purchases WHERE id=?",(purchase_id,)).fetchone()
            if not row or row["status"] in ("Refunded","Completed"): return False
            self.credit(c,row["user_id"],row["price"],"refund","purchase",str(purchase_id),None)
            c.execute("UPDATE purchases SET status='Refunded',updated_at=CURRENT_TIMESTAMP WHERE id=?",(purchase_id)); self.history(c,row["user_id"],"refund",reason); return True
    def create_payment(self,user_id,credits,stars):
        payment_id=uuid.uuid4().hex
        with self.db.transaction() as c:c.execute("INSERT INTO payments(id,user_id,credits,stars,status) VALUES(?,?,?,?, 'Pending')",(payment_id,user_id,credits,stars))
        return payment_id
    def validate_payment(self,payment_id,user_id,credits,stars):
        with self.db.connect() as c:return c.execute("SELECT 1 FROM payments WHERE id=? AND user_id=? AND credits=? AND stars=? AND status='Pending'",(payment_id,user_id,credits,stars)).fetchone() is not None
    def settle_payment(self,payment_id,user_id,charge_id):
        with self.db.transaction() as c:
            p=c.execute("SELECT * FROM payments WHERE id=?",(payment_id,)).fetchone()
            if not p or p["user_id"] != user_id or p["status"] != "Pending": return False
            c.execute("UPDATE payments SET status='Paid',telegram_charge_id=?,paid_at=CURRENT_TIMESTAMP WHERE id=?",(charge_id,payment_id))
            self.credit(c,user_id,p["credits"],"stars_payment","payment",payment_id,None); return True

class StorageManager:
    """Only this class owns repository capacity bookkeeping and GitHub calls."""
    def __init__(self, db, github, settings): self.db,self.github,self.settings=db,github,settings
    @staticmethod
    def archive_name(title, product_id):
        clean=re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', title).strip().rstrip('.')
        clean=re.sub(r'\s+', ' ', clean)[:80] or f"product-{product_id}"
        return f"{clean}.zip"
    def reserve_space(self, size):
        if size > self.settings.storage_max_file_bytes: raise ValueError("حجم الملف يتجاوز الحد المسموح")
        with self.db.transaction() as c:
            row=c.execute("SELECT * FROM storage_repositories WHERE is_active=1 AND used_bytes+reserved_bytes+?<=safe_bytes ORDER BY id LIMIT 1",(size,)).fetchone()
            if not row: raise ValueError("NO_STORAGE_REPOSITORY")
            c.execute("UPDATE storage_repositories SET reserved_bytes=reserved_bytes+? WHERE id=?",(size,row['id']))
            return dict(row)
    @staticmethod
    def file_name(title, product_id, original=None):
        if original:
            clean=re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', original).strip().rstrip('.')
            clean=re.sub(r'\s+', ' ', clean)[:80]
            if clean: return clean
        return StorageManager.archive_name(title, product_id)
    async def upload_product(self, product_id, data, filename=None):
        try:
            reservation=self.reserve_space(len(data))
        except ValueError as exc:
            if str(exc) != "NO_STORAGE_REPOSITORY": raise
            reservation=await self.create_repository_and_reserve(len(data))
        with self.db.connect() as c: product=c.execute("SELECT title FROM products WHERE id=?",(product_id,)).fetchone()
        if not product: raise ValueError("المنتج غير موجود")
        asset_name=filename or self.archive_name(product['title'],product_id)
        try:
            release_id,asset_id,asset_url=await self.github.upload_release_asset(reservation['repo_name'],f"product-{product_id}",asset_name,data)
            with self.db.transaction() as c:
                c.execute("UPDATE storage_repositories SET reserved_bytes=reserved_bytes-?,used_bytes=used_bytes+? WHERE id=?",(len(data),len(data),reservation['id']))
                c.execute("UPDATE products SET storage_repository_id=?,storage_path=?,storage_url=?,storage_asset_id=?,storage_release_id=?,file_size=?,is_active=1 WHERE id=?",(reservation['id'],asset_name,asset_url,asset_id,release_id,len(data),product_id))
        except Exception:
            with self.db.transaction() as c:c.execute("UPDATE storage_repositories SET reserved_bytes=reserved_bytes-? WHERE id=?",(len(data),reservation['id']))
            raise
    async def create_repository_and_reserve(self, size):
        with self.db.connect() as c:
            existing={r[0] for r in c.execute("SELECT repo_name FROM storage_repositories").fetchall()}
        try:
            existing |= await self.github.list_repositories()
        except Exception:
            log.warning("Could not list GitHub repositories; falling back to local database names",exc_info=True)
        prefix=self.settings.storage_repo_prefix
        number=1
        while True:
            name=f"{prefix}{number:03d}"
            if name in existing:
                number+=1; continue
            try:
                await self.github.create_repository(name); break
            except Exception as exc:
                status=getattr(getattr(exc,'response',None),'status_code',None)
                if status==422:
                    existing.add(name); number+=1; continue
                raise
        with self.db.transaction() as c:
            c.execute("INSERT OR IGNORE INTO storage_repositories(repo_name,max_bytes,safe_bytes) VALUES(?,?,?)",(name,self.settings.storage_max_bytes,self.settings.storage_safe_bytes))
        return self.reserve_space(size)
    async def sync_repositories(self):
        """Register any storage repos already on GitHub that are missing locally."""
        github_names=await self.github.list_repositories()
        prefix=self.settings.storage_repo_prefix
        with self.db.connect() as c:
            known={r[0] for r in c.execute("SELECT repo_name FROM storage_repositories").fetchall()}
        added=0
        with self.db.transaction() as c:
            for name in sorted(github_names):
                if name.startswith(prefix) and name not in known:
                    c.execute("INSERT OR IGNORE INTO storage_repositories(repo_name,max_bytes,safe_bytes,is_active) VALUES(?,?,?,1)",(name,self.settings.storage_max_bytes,self.settings.storage_safe_bytes))
                    added+=1
        return added
    async def connection_status(self):
        return await self.github.test_connection()
    async def download_product(self, product):
        with self.db.connect() as c:r=c.execute("SELECT repo_name FROM storage_repositories WHERE id=?",(product['storage_repository_id'],)).fetchone()
        if not r: raise ValueError("موقع الملف غير موجود")
        repo_name=r['repo_name']
        if product['storage_asset_id']:
            try:
                return await self.github.download_release_asset(repo_name,product['storage_asset_id'])
            except Exception as exc:
                status=getattr(getattr(exc,'response',None),'status_code',None)
                log.warning("Asset %s download failed (status %s), trying self-heal by tag",product['storage_asset_id'],status)
                # إصلاح ذاتي: الـasset_id قد يكون قديماً — نبحث عن الملف بالـtag والاسم ونحدّث القاعدة
                try:
                    release=await self.github.get_release_by_tag(repo_name,f"product-{product['id']}")
                    if release:
                        assets=await self.github.list_release_assets(repo_name,release["id"])
                        target=None
                        for asset in assets:
                            if asset.get("name")==product['storage_path']:
                                target=asset; break
                        if target is None and assets:
                            target=max(assets,key=lambda a: a.get("id",0))
                        if target:
                            with self.db.transaction() as c:
                                c.execute("UPDATE products SET storage_url=?,storage_asset_id=?,storage_release_id=? WHERE id=?",(target.get("browser_download_url"),target["id"],release["id"],product['id']))
                            return await self.github.download_release_asset(repo_name,target["id"])
                except Exception:
                    log.warning("Self-heal by tag failed",exc_info=True)
                raise
        # Legacy products used GitHub Contents. Migrate them to a private Release on first access.
        if not product['storage_path']:
            raise ValueError("المنتج لا يملك ملفاً صالحاً")
        data=await self.github.download_legacy_content(r['repo_name'],product['storage_path'])
        release_id,asset_id,asset_url=await self.github.upload_release_asset(r['repo_name'],f"product-{product['id']}",self.archive_name(product['title'],product['id']),data)
        with self.db.transaction() as c:
            c.execute("UPDATE products SET storage_url=?,storage_asset_id=?,storage_release_id=? WHERE id=?",(asset_url,asset_id,release_id,product['id']))
        return data
    async def delete_all_storage(self):
        with self.db.connect() as c: repositories=[r['repo_name'] for r in c.execute("SELECT repo_name FROM storage_repositories").fetchall()]
        for repo_name in repositories:
            await self.github.delete_repository(repo_name)
        with self.db.transaction() as c:
            c.execute("UPDATE products SET is_active=0,storage_repository_id=NULL,storage_path=NULL,storage_sha=NULL,storage_url=NULL,storage_asset_id=NULL,storage_release_id=NULL,file_size=NULL")
            c.execute("DELETE FROM storage_repositories")
    async def delete_product_storage(self, product):
        """يمسح ملفات المنتج من GitHub (الـasset ثم الـrelease لو فاضي) ويرجع المساحة المحجوزة."""
        repo_id=product['storage_repository_id']
        if not repo_id: return
        with self.db.connect() as c:r=c.execute("SELECT repo_name FROM storage_repositories WHERE id=?",(repo_id,)).fetchone()
        if not r: return
        repo_name=r['repo_name']
        freed=product['file_size'] or 0
        try:
            asset_id=product['storage_asset_id']; release_id=product['storage_release_id']
            if asset_id:
                try: await self.github.delete_release_asset(repo_name,asset_id)
                except Exception as exc:
                    if getattr(getattr(exc,'response',None),'status_code',None) != 404: raise
            if release_id:
                try:
                    assets=await self.github.list_release_assets(repo_name,release_id)
                    if not assets: await self.github.delete_release(repo_name,release_id)
                except Exception as exc:
                    if getattr(getattr(exc,'response',None),'status_code',None) != 404: raise
        except Exception:
            log.warning("Could not delete GitHub files for product %s",product['id'],exc_info=True)
        finally:
            if freed:
                with self.db.transaction() as c:
                    c.execute("UPDATE storage_repositories SET used_bytes=CASE WHEN used_bytes>=? THEN used_bytes-? ELSE 0 END WHERE id=?",(freed,freed,repo_id))
