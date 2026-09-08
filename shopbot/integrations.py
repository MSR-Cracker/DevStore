import base64, httpx

class ImageService:
    def __init__(self, key): self.key=key
    async def upload(self, data: bytes) -> str:
        if not self.key: raise RuntimeError("IMGBB_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=60) as client:
            r=await client.post("https://api.imgbb.com/1/upload",params={"key":self.key},data={"image":base64.b64encode(data).decode()}); r.raise_for_status(); return r.json()["data"]["url"]

class GitHubStorage:
    def __init__(self, token, owner): self.token,self.owner=token,owner
    @property
    def headers(self): return {"Authorization":f"Bearer {self.token}","Accept":"application/vnd.github+json"}
    async def get_release_by_tag(self, repo, tag):
        async with httpx.AsyncClient(timeout=60) as client:
            r=await client.get(f"https://api.github.com/repos/{self.owner}/{repo}/releases/tags/{tag}",headers=self.headers)
            if r.status_code==404: return None
            r.raise_for_status(); return r.json()
    async def list_release_assets(self, repo, release_id):
        async with httpx.AsyncClient(timeout=60) as client:
            r=await client.get(f"https://api.github.com/repos/{self.owner}/{repo}/releases/{release_id}/assets",headers=self.headers)
            r.raise_for_status(); return r.json()
    async def delete_release_asset(self, repo, asset_id):
        async with httpx.AsyncClient(timeout=60) as client:
            r=await client.delete(f"https://api.github.com/repos/{self.owner}/{repo}/releases/assets/{asset_id}",headers=self.headers)
            r.raise_for_status()
    async def upload_release_asset(self, repo, tag, asset_name, data):
        if not self.token or not self.owner: raise RuntimeError("GitHub storage is not configured")
        async with httpx.AsyncClient(timeout=300) as client:
            release=await client.post(f"https://api.github.com/repos/{self.owner}/{repo}/releases",headers=self.headers,json={"tag_name":tag,"name":tag,"draft":False,"prerelease":False})
            if release.status_code==422:
                # الـRelease بنفس الـtag موجود مسبقاً (إعادة رفع ملف) — نعيد استخدامه بدل الفشل
                release_data=await self.get_release_by_tag(repo,tag)
                if not release_data: raise RuntimeError(f"Release tag {tag} already exists but could not be fetched")
            else:
                release.raise_for_status(); release_data=release.json()
            upload_url=release_data["upload_url"].split("{")[0]
            try:
                asset=await client.post(upload_url,params={"name":asset_name},headers={**self.headers,"Content-Type":"application/octet-stream"},content=data); asset.raise_for_status()
            except Exception as exc:
                status=getattr(getattr(exc,'response',None),'status_code',None)
                if status==422:
                    # ملف بنفس الاسم موجود — نحذفه ونعيد الرفع
                    for existing in await self.list_release_assets(repo,release_data["id"]):
                        if existing.get("name")==asset_name:
                            await self.delete_release_asset(repo,existing["id"]); break
                    asset=await client.post(upload_url,params={"name":asset_name},headers={**self.headers,"Content-Type":"application/octet-stream"},content=data); asset.raise_for_status()
                else: raise
            asset_data=asset.json()
            return release_data["id"],asset_data["id"],asset_data["browser_download_url"]
    async def create_repository(self, name):
        if not self.token or not self.owner: raise RuntimeError("GitHub storage is not configured")
        async with httpx.AsyncClient(timeout=60) as client:
            r=await client.post("https://api.github.com/user/repos",headers=self.headers,json={"name":name,"private":True,"description":"Shop Bot product storage","auto_init":True}); r.raise_for_status(); return r.json()["name"]
    async def delete_release(self, repo, release_id):
        async with httpx.AsyncClient(timeout=60) as client:
            r=await client.delete(f"https://api.github.com/repos/{self.owner}/{repo}/releases/{release_id}",headers=self.headers)
            r.raise_for_status()
    async def delete_repository(self, name):
        async with httpx.AsyncClient(timeout=60) as client:
            r=await client.delete(f"https://api.github.com/repos/{self.owner}/{name}",headers=self.headers)
            r.raise_for_status()
    async def test_connection(self):
        if not self.token or not self.owner: return False, "GitHub storage غير مُكرّى على إعدادات"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r=await client.get("https://api.github.com/user",headers=self.headers)
                if r.status_code==200:
                    return True, f"متصل كـ @{r.json().get('login','?')}"
                return False, f"HTTP {r.status_code}"
        except Exception as exc:
            return False, str(exc)[:120]
    async def list_repositories(self):
        if not self.token: raise RuntimeError("GitHub storage is not configured")
        names=set(); page=1
        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                r=await client.get(f"https://api.github.com/user/repos?type=all&per_page=100&page={page}",headers=self.headers)
                if r.status_code==404: break
                r.raise_for_status(); data=r.json()
                names |= {repo["name"] for repo in data}
                if len(data)<100: break
                page+=1
        return names
    async def download_release_asset(self,repo,asset_id):
        async with httpx.AsyncClient(timeout=120,follow_redirects=True) as client:
            r=await client.get(f"https://api.github.com/repos/{self.owner}/{repo}/releases/assets/{asset_id}",headers={**self.headers,"Accept":"application/octet-stream"}); r.raise_for_status(); return r.content
    async def download_legacy_content(self, repo, path):
        """Temporary compatibility reader for products stored before Release Assets."""
        async with httpx.AsyncClient(timeout=120) as client:
            r=await client.get(f"https://api.github.com/repos/{self.owner}/{repo}/contents/{path}",headers=self.headers)
            r.raise_for_status()
            return base64.b64decode(r.json()["content"])
