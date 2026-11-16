# -*- coding: utf-8 -*-
"""初中免费试卷下载器 - 仅检索和下载公开可访问的完整文件。"""
import os, re, sys, json, hashlib, threading, queue, time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse, unquote
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    requests = None
    BeautifulSoup = None

APP_NAME = "初中免费试卷下载器"
SUBJECTS = ["语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理", "道德与法治"]
GRADES = ["七年级", "八年级", "九年级"]
START_DATE = datetime(2025, 1, 1)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(s))
    return s.strip(" ._")[:100] or "未命名试卷"


def extract_date(text):
    """只接受文本中明确出现的日期；不把搜索引擎索引日期当发布日期。"""
    patterns = [r'(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?', r'(20\d{2})(\d{2})(\d{2})']
    for pat in patterns:
        for m in re.finditer(pat, text or ""):
            try:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if START_DATE <= d <= datetime.now():
                    return d.strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_file(path):
    """验证常见文档容器结构；不能仅凭结构保证每页内容完整。"""
    p = Path(path)
    ext = p.suffix.lower()
    if p.stat().st_size < 2048:
        return False, "文件过小，疑似网页错误页或不完整文件"
    with open(p, "rb") as f:
        head = f.read(16)
        f.seek(max(0, p.stat().st_size - 4096))
        tail = f.read()
    if ext == ".pdf":
        ok = head.startswith(b"%PDF-") and b"%%EOF" in tail
        return ok, "PDF 文件头和结束标记通过" if ok else "PDF 结构校验失败"
    if ext == ".docx":
        import zipfile
        try:
            with zipfile.ZipFile(p) as z:
                ok = "[Content_Types].xml" in z.namelist() and "word/document.xml" in z.namelist() and z.testzip() is None
            return ok, "DOCX 文档结构通过" if ok else "DOCX 结构校验失败"
        except Exception:
            return False, "DOCX 压缩包损坏或格式不正确"
    if ext == ".doc":
        ok = head.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
        return ok, "DOC 文件头通过" if ok else "DOC 文件头校验失败"
    return False, "不支持的文件类型"


class Searcher:
    def __init__(self, log):
        self.log = log

    def search(self, grade, subject, keyword, max_items=30):
        if requests is None or BeautifulSoup is None:
            raise RuntimeError("缺少依赖。请先运行 install_requirements.bat")
        base = f'{grade} {subject} {keyword} 试卷 (filetype:pdf OR filetype:doc OR filetype:docx)'
        # 分站检索公开索引：站点不可用时记录日志并继续，不使用登录态或付费账号。
        queries = [base, base + ' site:zxxk.com', base + ' site:zujuan.xkw.com', base + ' site:basic.smartedu.cn']
        results, seen = [], set()
        for q in queries:
            url = "https://www.bing.com/search?format=rss&q=" + quote(q)
            self.log(f"正在搜索：{q}")
            try:
                r = requests.get(url, headers=HEADERS, timeout=25)
                r.raise_for_status()
                soup = BeautifulSoup(r.content, "xml")
                for item in soup.find_all("item")[:max_items]:
                    title = item.title.get_text(" ", strip=True) if item.title else "未命名结果"
                    link = item.link.get_text(strip=True) if item.link else ""
                    desc = item.description.get_text(" ", strip=True) if item.description else ""
                    date = extract_date(title + " " + desc)
                    # 严格筛选：结果文字必须有明确的 2025 年及以后日期证据。
                    if not date or not link or link in seen:
                        continue
                    seen.add(link)
                    results.append({"title": title, "url": link, "description": desc, "date": date})
            except Exception as e:
                self.log(f"该搜索渠道暂不可用，继续其他渠道：{e}")
        self.log(f"搜索完成，找到 {len(results)} 条带明确日期证据的去重结果。")
        return results

    def find_downloads(self, page_url):
        parsed = urlparse(page_url)
        if parsed.scheme not in ("http", "https"):
            return []
        if re.search(r'\.(pdf|docx?|PDF|DOCX?)($|[?#])', page_url):
            return [page_url]
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=25, allow_redirects=True)
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "").lower()
            if any(x in ctype for x in ("application/pdf", "msword", "officedocument.wordprocessingml")):
                return [r.url]
            soup = BeautifulSoup(r.text, "html.parser")
            found = []
            for a in soup.find_all("a", href=True):
                u = urljoin(r.url, a["href"])
                if re.search(r'\.(pdf|docx?|PDF|DOCX?)($|[?#])', u):
                    found.append(u)
            # Remove duplicates, preserve order.
            return list(dict.fromkeys(found))[:10]
        except Exception as e:
            self.log(f"无法读取页面 {page_url}：{e}")
            return []


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1050x700")
        self.minsize(850, 580)
        self.results = []
        self.msgq = queue.Queue()
        self.save_root = tk.StringVar(value=str(Path.home() / "Documents" / "初中免费试卷"))
        self.grade = tk.StringVar(value="七年级")
        self.subject = tk.StringVar(value="数学")
        self.keyword = tk.StringVar(value="单元测试")
        self.status = tk.StringVar(value="就绪。仅下载公开可访问的 PDF / DOC / DOCX 文件。")
        self._build()
        self.after(150, self._poll)

    def _build(self):
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="年级").grid(row=0, column=0, sticky="w")
        ttk.Combobox(top, textvariable=self.grade, values=GRADES, state="readonly", width=12).grid(row=0, column=1, padx=6)
        ttk.Label(top, text="学科").grid(row=0, column=2, sticky="w")
        ttk.Combobox(top, textvariable=self.subject, values=SUBJECTS, state="readonly", width=14).grid(row=0, column=3, padx=6)
        ttk.Label(top, text="关键词").grid(row=0, column=4, sticky="w")
        ttk.Entry(top, textvariable=self.keyword, width=28).grid(row=0, column=5, padx=6)
        self.search_btn = ttk.Button(top, text="搜索试卷", command=self.start_search)
        self.search_btn.grid(row=0, column=6, padx=8)
        ttk.Label(top, text="保存位置").grid(row=1, column=0, pady=(12, 0), sticky="w")
        ttk.Entry(top, textvariable=self.save_root).grid(row=1, column=1, columnspan=5, sticky="ew", padx=6, pady=(12, 0))
        ttk.Button(top, text="选择文件夹", command=self.choose_folder).grid(row=1, column=6, pady=(12, 0))
        top.columnconfigure(5, weight=1)

        note = ttk.Label(self, text="筛选规则：只保留搜索标题/摘要中明确标注 2025-01-01 及以后日期的结果；下载前会检查文件类型和结构。", foreground="#555")
        note.pack(anchor="w", padx=14, pady=(0, 8))

        mid = ttk.Frame(self, padding=(12, 0, 12, 8))
        mid.pack(fill="both", expand=True)
        cols = ("date", "title", "url", "state")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        for c, t, w in [("date", "日期证据", 100), ("title", "搜索结果", 330), ("url", "来源页面", 430), ("state", "状态", 120)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        y = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        x = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1); mid.columnconfigure(0, weight=1)

        bar = ttk.Frame(self, padding=(12, 0, 12, 8)); bar.pack(fill="x")
        ttk.Button(bar, text="下载选中", command=self.download_selected).pack(side="left")
        ttk.Button(bar, text="批量下载全部", command=self.download_all).pack(side="left", padx=8)
        ttk.Button(bar, text="打开保存目录", command=self.open_folder).pack(side="left")
        ttk.Label(bar, textvariable=self.status).pack(side="right")

        logf = ttk.LabelFrame(self, text="运行日志", padding=8); logf.pack(fill="x", padx=12, pady=(0, 12))
        self.logbox = tk.Text(logf, height=7, wrap="word", state="disabled")
        self.logbox.pack(fill="x")

    def log(self, msg): self.msgq.put(("log", msg))
    def _poll(self):
        try:
            while True:
                kind, val = self.msgq.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal"); self.logbox.insert("end", f"[{time.strftime('%H:%M:%S')}] {val}\n"); self.logbox.see("end"); self.logbox.configure(state="disabled")
                elif kind == "search_done":
                    self.results = val; self.tree.delete(*self.tree.get_children())
                    for i, item in enumerate(val):
                        self.tree.insert("", "end", iid=str(i), values=(item["date"], item["title"], item["url"], "待检查下载"))
                    self.search_btn.configure(state="normal"); self.status.set(f"搜索完成：{len(val)} 条结果")
                elif kind == "state": self.status.set(val)
                elif kind == "row_state":
                    iid, state = val
                    if self.tree.exists(iid):
                        vals = list(self.tree.item(iid, "values")); vals[3] = state; self.tree.item(iid, values=vals)
        except queue.Empty: pass
        self.after(150, self._poll)

    def choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.save_root.get() or str(Path.home()))
        if d: self.save_root.set(d)

    def start_search(self):
        if not self.keyword.get().strip():
            messagebox.showinfo("提示", "请输入搜索关键词。")
            return
        self.search_btn.configure(state="disabled"); self.status.set("正在搜索……")
        def work():
            try:
                results = Searcher(self.log).search(self.grade.get(), self.subject.get(), self.keyword.get().strip())
                self.msgq.put(("search_done", results))
            except Exception as e:
                self.log(f"搜索失败：{e}"); self.msgq.put(("search_done", [])); self.msgq.put(("state", "搜索失败，请检查网络或依赖"))
        threading.Thread(target=work, daemon=True).start()

    def _download_items(self, indices):
        root = Path(self.save_root.get()) / self.grade.get() / self.subject.get()
        root.mkdir(parents=True, exist_ok=True)
        searcher = Searcher(self.log)
        for idx in indices:
            if idx >= len(self.results): continue
            item = self.results[idx]
            self.msgq.put(("row_state", (str(idx), "查找文件链接")))
            urls = searcher.find_downloads(item["url"])
            if not urls:
                self.log(f"未找到公开直链：{item['title']}（可能需要登录/付费，或页面未提供文件链接）")
                self.msgq.put(("row_state", (str(idx), "无公开文件链接")))
                continue
            saved = False
            for file_url in urls:
                ext = Path(urlparse(file_url).path).suffix.lower()
                if ext not in (".pdf", ".doc", ".docx"):
                    continue
                name = safe_name(item["date"] + "_" + item["title"]) + ext
                dest = root / name
                if dest.exists(): dest = root / (dest.stem + "_" + hashlib.md5(file_url.encode()).hexdigest()[:6] + ext)
                try:
                    self.log(f"开始下载：{file_url}")
                    with requests.get(file_url, headers=HEADERS, timeout=45, stream=True) as resp:
                        resp.raise_for_status()
                        ctype = resp.headers.get("Content-Type", "").lower()
                        if "text/html" in ctype:
                            raise ValueError("服务器返回网页而不是文档文件")
                        with open(dest, "wb") as f:
                            for chunk in resp.iter_content(1024 * 128):
                                if chunk: f.write(chunk)
                    ok, reason = validate_file(dest)
                    if not ok:
                        dest.unlink(missing_ok=True); self.log(f"文件未通过完整性结构检查：{reason}"); continue
                    digest = sha256_file(dest)
                    meta = {"title": item["title"], "date_evidence": item["date"], "source_page": item["url"], "download_url": file_url, "file": dest.name, "bytes": dest.stat().st_size, "sha256": digest, "validation": reason, "downloaded_at": datetime.now().isoformat(timespec="seconds"), "note": "结构检查通过不等于人工逐页核对；请打开文件确认页数和内容完整。"}
                    dest.with_suffix(dest.suffix + ".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    self.log(f"下载成功：{dest}；{reason}"); saved = True
                except Exception as e:
                    self.log(f"下载失败：{e}"); dest.unlink(missing_ok=True) if dest.exists() else None
            self.msgq.put(("row_state", (str(idx), "已下载" if saved else "下载失败/未验证")))
        self.msgq.put(("state", "批量任务结束"))

    def download_selected(self):
        sel = self.tree.selection()
        if not sel: messagebox.showinfo("提示", "请先选择要下载的试卷。"); return
        indices = [int(x) for x in sel]
        threading.Thread(target=self._download_items, args=(indices,), daemon=True).start()

    def download_all(self):
        if not self.results: messagebox.showinfo("提示", "请先搜索试卷。"); return
        threading.Thread(target=self._download_items, args=(list(range(len(self.results))),), daemon=True).start()

    def open_folder(self):
        p = Path(self.save_root.get())
        p.mkdir(parents=True, exist_ok=True)
        try: os.startfile(str(p))
        except Exception: messagebox.showinfo("保存目录", str(p))


if __name__ == "__main__":
    App().mainloop()
