#!/usr/bin/env python3
"""Static self-check for the dashboard.

The UI is vanilla JS wired together by string ids and inline onclick attributes, so a
rename breaks it silently: the page still loads, the button just does nothing. These four
checks catch exactly that class of breakage.

    python3 static/orchestrator/check_ui.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# encoding="utf-8" BẮT BUỘC: không truyền thì read_text() theo locale, và trên Windows locale
# là cp1252 — index.html/app.js có comment tiếng Việt, gạch dài, ký tự khung nên vỡ ngay
# ở CI (UnicodeDecodeError), dù chạy tốt trên Linux.
HTML = (HERE / "index.html").read_text(encoding="utf-8")
JS = (HERE / "app.js").read_text(encoding="utf-8")

# Cùng lý do cho chiều ra: tên rule CSS in ra khi FAIL có thể chứa non-ASCII, console
# Windows mặc định cp1252 sẽ ném UnicodeEncodeError và giấu mất chính cái lỗi cần đọc.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # stream bị thay bằng thứ không reconfigure được
        pass

# Tên có sẵn của trình duyệt/JS + từ khoá — xuất hiện trong onclick nhưng không phải hàm của app.
BUILTIN = {
    "confirm", "alert", "prompt", "getElementById", "stopPropagation",
    "parseInt", "parseFloat", "String", "Number", "Boolean", "JSON",
    "if", "for", "while", "return", "typeof",
}

fails = []


def check(name, bad, hint):
    if bad:
        fails.append(f"{name}: {hint}\n    " + "\n    ".join(sorted(bad)))
    print(("FAIL " if bad else "ok   ") + name)


# 1 · Mọi $("id") trong JS phải tồn tại trong HTML.
html_ids = set(re.findall(r'id="([^"]+)"', HTML))
js_ids = set(re.findall(r'\$\("([^"]+)"\)', JS))
# Các id do chính JS sinh ra rồi mới truy vấn lại.
js_made = set(re.findall(r'\bid\s*=\s*"([\w-]+)"', JS)) | set(
    re.findall(r'<(?:svg|div|textarea|span)\s+id="([\w-]+)"', JS)
)
check("every $(\"id\") exists in index.html", js_ids - html_ids - js_made,
      "referenced by app.js but no such element")

# 2 · Mọi hàm gọi trong handler inline (onclick/onchange/oninput/onfocus/onmousedown…) phải
# được gán vào window ở app.js. KHÔNG chỉ onclick: ô chọn
# phiên chạy bằng onchange/onmousedown — sót attribute nào là loại đó không được kiểm, và một cái tên
# gõ sai ở đó im lặng y hệt onclick (element vẫn hiện, gõ vào thì không có gì xảy ra).
# KHÔNG neo ^: app.js gán nhiều hàm trên cùng một dòng.
exported = set(re.findall(r"\bwindow\.(\w+)\s*=", JS))
called = set()
for src in (HTML, JS):
    for body in re.findall(r'\bon[a-z]+="([^"]*)"', src):
        # ${...} chạy lúc render trong scope JS, không phải trong onclick → bỏ đi.
        # (?<![.\w$]) bỏ lời gọi phương thức (this.value.trim()) — chỉ còn hàm toàn cục.
        called |= set(re.findall(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(",
                                 re.sub(r"\$\{[^}]*\}", "", body)))
check("every inline handler is exported", called - exported - BUILTIN,
      "called from an inline on*= handler but never assigned to window.*")

# 3 · Mọi icon <use href="#i-x"> phải có <symbol> tương ứng trong sprite.
symbols = set(re.findall(r'<symbol id="(i-[\w-]+)"', HTML))
used = set(re.findall(r'href="#(i-[\w-]+)"', HTML)) | {
    "i-" + n for n in re.findall(r'ic\("([\w-]+)"', JS)
}
check("every icon exists in the sprite", used - symbols, "no <symbol> with this id")

# 4 · Không màu hardcode ngoài khối token — hardcode là hỏng dark mode âm thầm.
style = HTML.split("<style>", 1)[1].split("</style>", 1)[0]
tokens_end = style.rindex("--ring:")
body_css = style[style.index("}", tokens_end):]
stray = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", body_css)) | set(
    re.findall(r"rgba?\([^)]*\)", body_css)
)
# VS Code tự sơn nền của nó.
stray -= {"#1e1e1e"}
check("no hardcoded colors outside the token block", stray,
      "put it in :root / :root[data-theme=dark] instead")

# 5 · Mỗi .term-slot phải mang data-key riêng. Card agent và card editor của CÙNG một session
# dùng chung sổ cvTerms; trùng key là hai card cùng trỏ vào một xterm, và cái sau cướp host của
# cái trước — trên màn hình là một card bỗng trống trơn, không lỗi, không log.
slots = re.findall(r'<div class="term-slot"([^>]*)>', JS)
check("every terminal slot carries a data-key",
      {s.strip() for s in slots if "data-key" not in s},
      "attachTerms keys cvTerms by data-key; without it the agent terminal and the editor of the "
      "same session collide on one xterm")

# 6 · Bản xterm vendor phải còn miếng vá toạ độ chuột. Card nằm trong #world có transform:scale(k);
# bản gốc của xterm trộn pixel màn hình (getBoundingClientRect) với cell size dạng CSS, nên mọi
# mức zoom khác 100% là bôi chọn trúng nhầm dòng — lệch = hàng × |1-k|, càng xa mép trên càng sai.
# Nâng cấp xterm sẽ ghi đè im lặng: không có chốt này thì bug quay lại mà không ai biết.
XTERM = (HERE / "vendor" / "xterm.js").read_text(encoding="utf-8")
missing = set()
if "PATCHED-FOR-ORCHESTRATOR" not in XTERM:
    missing.add("marker comment")
if "/u-n,(t.clientY-s.top)/d-o]" not in XTERM:
    missing.add("the divide-by-scale expression in getCoordsRelativeToElement")
check("the vendored xterm keeps its mouse-coordinate patch", missing,
      "re-apply it after upgrading xterm, or selection breaks at every zoom level except 100%")

# 7 · Card editor bị KHOÁ vào cạnh phải card terminal: vị trí + chiều cao là suy ra, không lưu.
# Ba mảnh phải đi cùng nhau, thiếu một là hỏng im lặng — thiếu snapPairs thì card nằm ở (0,0);
# thiếu lời gọi trong layoutZones thì nó đứng yên trong lúc kéo rồi mới nhảy về chỗ; thiếu chốt
# trong saveNodeGeom thì store còn x/y cũ và card nhấp một cái ở khung hình đầu mỗi lần render.
lost = set()
if "function snapPairs(" not in JS:
    lost.add("snapPairs() — the function that glues the editor to its terminal")
if "function layoutZones() {\n  snapPairs();" not in JS:
    lost.add("layoutZones() must call snapPairs() first, or the editor lags every drag")
if "cvPairOf[el.dataset.nid]" not in JS:
    lost.add("saveNodeGeom must drop x/y/h for a paired editor (they are derived)")
if "cvPairOf[node.dataset.nid] || node" not in JS:
    lost.add("pointerdown must redirect a paired editor's drag to its terminal card")
if "byTerm" not in JS or "nids.sort()" not in JS:
    lost.add("snapPairs must lay several paired cards out in a ROW: an editor and a folder card "
             "on the same terminal would otherwise land on the exact same spot")
check("cards paired to a terminal are locked to it", lost,
      "a paired card has no geometry of its own; only the pair moves")

# 8 · Khung cặp (terminal + editor của cùng session) phải kéo giãn được. .group-zone là
# pointer-events:none, nên tay nắm .rz bên trong nó CHẾT nếu CSS không bật lại — trên màn hình
# khung vẫn vẽ ra, góc vẫn có tam giác, kéo thì không có gì xảy ra. Đúng kiểu hỏng im lặng.
gaps = set()
if ".pair-zone .rz" not in style or "pointer-events: auto" not in style.split(".pair-zone .rz", 1)[1][:80]:
    gaps.add(".pair-zone .rz needs pointer-events:auto (the zone itself is pointer-events:none)")
if 'class="node group-zone pair-zone"' not in JS:
    gaps.add("pairZoneHtml must keep both group-zone (layout/drag) and pair-zone (style) classes")
if '"gresize"' not in JS:
    gaps.add("the pointerdown/pointermove branch that scales every member of the group")
check("the pair frame can be dragged and resized", gaps,
      "resizing the frame is the whole point of grouping the two cards")

# 9 · Shift+wheel phải zoom được kể cả khi con trỏ nằm trên terminal — đó là đường DUY NHẤT để
# zoom khi một card phủ kín canvas. Hai mảnh dễ bị "dọn cho gọn" mà hỏng im lặng:
#   - bỏ capture:true → xterm nhận wheel trước, Shift+wheel với nó là cuộn nhanh, nên terminal
#     vừa cuộn vừa zoom một lúc;
#   - rút `e.deltaY || e.deltaX` về mỗi deltaY → Chrome/Safari đổi trục khi giữ Shift (deltaY=0),
#     bánh xe quay mà mức zoom đứng im.
wheel = JS[JS.index('cv.addEventListener("wheel"'):][:1400]
gaps = set()
if "capture: true" not in wheel:
    gaps.add("capture:true — without it xterm fast-scrolls under the zoom")
if "e.deltaY || e.deltaX" not in wheel:
    gaps.add("e.deltaY || e.deltaX — browsers move shift+wheel onto the X axis")
if "!e.shiftKey && e.target.closest" not in wheel:
    gaps.add("the shift bypass of the .term-slot guard")
if "stopPropagation" not in wheel:
    gaps.add("stopPropagation — the terminal would still scroll a notch")
check("shift+wheel zooms even over a terminal", gaps,
      "a card covering the canvas leaves no background to scroll on")

# 10 · Màn Home dùng MỘT grid cho hai tầng (group / workspace của group). Hỏng im lặng ở đây là
# lọc bị bỏ đi: grid lại liệt kê MỌI workspace (hoặc sidebar thành một danh sách phẳng), trông vẫn chạy, nhưng group thành
# vô nghĩa — và ở máy nhiều workspace thì đó đúng là cái nó sinh ra để chữa.
gaps = set()
if "function renderHome()" not in JS:
    gaps.add("renderHome() — the one renderer both Home layers go through")
if "wsOfGroup = (gid)" not in JS:
    gaps.add("wsOfGroup() — the filter that makes a group mean anything")
if "GROUPS.map((g) =>" not in JS or "all = wsOfGroup(g.id)" not in JS or "all.map((w) => row(" not in JS:
    gaps.add("renderShellTabs must list workspaces UNDER their group heading, not one flat list")
# Cả tiêu đề group là nút thu/mở (mũi tên nhỏ một mình thì người dùng bấm tên rồi tưởng hỏng).
if "class=\"sb-grp\" onclick=\"toggleGroupFold(" not in JS:
    gaps.add("the whole sidebar group heading must fold/unfold its workspaces")
if "renderWorkspaceGrid" in JS:
    gaps.add("renderWorkspaceGrid is gone — a call left behind renders the old flat list")
for fn in ("openGroup", "homeNew", "moveWsGroup"):
    if f"window.{fn} = {fn}" not in JS:
        gaps.add(f"{fn} must be exported — it is only reachable from an inline handler")
check("the Home screen keeps its group layer", gaps,
      "a group is a filter; drop the filter and it is decoration")

# 11 · Modal MCP ghi cho MỌI CLI đang cài, nên /api/mcp trả {clis, servers} chứ không còn là một
# mảng trần. Đọc nhầm shape thì modal hiện "Nothing registered yet" trên một máy đầy server.
gaps = set()
if 'const r = await api("/api/mcp");' not in JS or "r.servers" not in JS:
    gaps.add("mcpLoad must read r.servers — /api/mcp is {clis, servers}, not an array")
if "mcpClis = r.clis" not in JS:
    gaps.add("mcpClis — the per-row tags have nothing to compare against without it")
if "cli-tag" not in JS or ".cli-tag" not in style:
    gaps.add("the per-CLI tag that shows which CLIs actually have a server")
check("the MCP modal speaks for every CLI", gaps,
      "an agent on agy reads a different file than claude")

# 12 · Mọi card agent có CẢ nút editor LẪN nút thư mục, không cần cờ nào. Không còn chế độ dev.
gaps = set()
if "dev_mode" in JS or "DEV" in re.findall(r"\bDEV\b", JS):
    gaps.add("the DEV branch must stay gone: the buttons no longer depend on a server flag")
if "window.openFolderCard = openFolderCard" not in JS or "window.openEditor = openEditor" not in JS:
    gaps.add("openEditor/openFolderCard must be exported — only inline handlers reach them")
if "`openEditor('${id}')`" not in JS or "`openFolderCard('${id}')`" not in JS:
    gaps.add("the card must offer both the editor button and the folder button")
check("every agent card has both the editor and the folder button", gaps,
      "no flag should decide who can open the editor")

# 12b · Card editor: những thứ chỉ hỏng khi người thật dùng.
gaps = set()
if 'registerOscHandler(52' not in JS or '"ed:"' not in JS:
    gaps.add("OSC 52 handler on ed: terminals — Ctrl+C in nvim never reaches the browser clipboard")
if "readText" in JS.split("registerOscHandler(52")[1].split("});")[0]:
    gaps.add("the OSC 52 handler must stay write-only: a program must not read the clipboard")
if "wireEditorTerm(t)" not in JS or "t.key.startsWith(\"ed:\")" not in JS:
    gaps.add("wireEditorTerm must only run on editor terminals, agents keep their own Ctrl+C/Ctrl+V")
if '"Symbols Nerd Font Mono"' not in style or "SymbolsNerdFontMono-Regular.woff2" not in style:
    gaps.add("@font-face for the icon font — without it the file tree draws empty boxes")
font = HERE / "vendor" / "fonts" / "SymbolsNerdFontMono-Regular.woff2"
if not font.is_file() or not (HERE / "vendor" / "fonts" / "SymbolsNerdFont-LICENSE.txt").is_file():
    gaps.add("the bundled icon font and its licence")
if 'document.fonts.load(\'12px "Symbols Nerd Font Mono"\'' not in JS:
    gaps.add("xterm must wait for the icon font or the first frame has empty boxes")
if "edView[st.session]" not in JS or 'i === 0 ? "on"' in JS:
    gaps.add("the highlighted tab must come from edView — the first tab must not win on re-render")
if "Source Control" not in JS or '"Files"' not in JS:
    gaps.add("the head tabs say Files and Source Control")
if "ed-status" not in JS or ".ed-status" not in style or "Downloading the editor" not in JS:
    gaps.add("text on the card during the first download, not a black rectangle")
check("the editor card keeps clipboard, icons, tab highlight and download text", gaps,
      "these only break in front of a real user")

# 13 · Card thư mục đọc cây qua HTTP và nằm trên canvas, nên nó dính hai cái bẫy riêng:
#   - path nhồi vào inline onclick: một dấu nháy trong tên file là phá cả attribute, và tên file
#     thì do project quyết chứ không do mình;
#   - .fld-body phải có mặt ở CẢ guard của listener wheel LẪN CSS (overflow). Thiếu guard thì lăn
#     chuột trong danh sách là zoom canvas; thiếu overflow thì danh sách dài không cuộn được.
gaps = set()
if "data-fdir" not in JS or "data-ffile" not in JS:
    gaps.add("folder and file paths belong in data attributes, not in an inline onclick")
if 'addEventListener("click", folderClick)' not in JS:
    gaps.add("the delegated folderClick listener — cards are rebuilt on every canvas render")
if "data-fcrumb" not in JS or "folderCrumbHtml(sid)" not in JS:
    gaps.add("folderPaint must redraw the breadcrumb: it sits in the header, outside .fld-body, "
             "so painting only the body leaves the path stuck at the project root")
if '".term-slot, .fld-body, .cv-overlay"' not in JS:
    gaps.add(".fld-body in the wheel guard — scrolling the list would zoom the canvas instead")
if ".fld-body" not in style or "overflow: auto" not in style:
    gaps.add(".fld-body { overflow: auto } — a long listing would have no way to scroll")
if '"/api/folder/list"' not in JS and "/api/folder/list?session=" not in JS:
    gaps.add("the call to /api/folder/list — the card has nothing to draw without it")
if "window.openFolder = openFolder" not in JS:
    gaps.add("openFolder stays exported — the card header offers the OS file manager as a way out")
# Dán cặp: ba mảnh, thiếu một là hỏng im lặng — không đăng ký thì card trôi tự do; không có
# .sized thì danh sách giữ 340px trong một khung cao hơn và chừa mảng trống; không có .paired thì
# chỗ giáp hai card hở ra hai đường bo.
if "cvPairOf[nid] = term;" not in JS:
    gaps.add("the folder node must register in cvPairOf, or it is not glued to its terminal")
if ".node.sized > .folder-card" not in style or ".node.sized .folder-card .fld-body" not in style:
    gaps.add(".node.sized rules for .folder-card — its height is derived, the list must fill it")
if ".node.paired > .folder-card" not in style:
    gaps.add(".node.paired > .folder-card — the seam needs square corners, not two curves")
check("the folder card browses and scrolls inside the canvas", gaps,
      "it replaced a window that opened outside the orchestrator")

# 14 · Form spawn phải đi HAI bước: group rồi mới workspace. Hỏng im lặng theo hai kiểu:
#   - quay lại danh sách phẳng mọi workspace của cả máy — vẫn spawn được, chỉ là dễ spawn nhầm
#     chỗ, đúng thứ group sinh ra để chữa;
#   - workspace lại có mặc định. Card 'default' đã bỏ khỏi form, nên không chọn mà vẫn gửi được
#     là agent rơi vào workspace fallback, nơi không hiện trên màn Home.
gaps = set()
if 'pickCard("group"' not in JS:
    gaps.add("the group step — without it the form is a flat list again")
if "wsOfGroup(spSel.group)" not in JS:
    gaps.add("the workspace list must be filtered by the picked group")
if "spSel = { group:" not in JS:
    gaps.add("spSel must carry the picked group")
if '!spSel.ws ? "Pick a workspace"' not in JS:
    gaps.add("workspace has no default any more, so the form must refuse an empty one")
if 'name: "default"' in JS:
    gaps.add("the 'default' workspace card is gone — a leftover puts it back in the picker")
if "wsOfGroup(g.id).length" not in JS:
    gaps.add("a group card must count the workspaces the grid actually shows, "
             "not the server's count of every row")
check("spawning goes through a group first", gaps,
      "a flat list of every workspace on the machine is what groups exist to replace")

# 15 · Ctrl+K phải mở palette ở MỌI chỗ, kể cả khi focus nằm trong terminal của pane. Hỏng im lặng:
#   - bỏ capture:true → xterm nhận phím trước, terminal gõ ra "k" (hoặc xoá dòng) và palette không mở;
#   - mất relay {t:"palette"} pane→shell → Ctrl+K chỉ chạy ở màn Home, trong workspace thì im;
#   - mất {t:"focus"} → chọn agent trong palette mở đúng workspace nhưng không chọn card nào.
gaps = set()
if 'addEventListener("keydown", (e) => { if (palKey(e)) palOpen(); }, { capture: true })' not in JS:
    gaps.add("the shell's capture-phase Ctrl+K listener")
if 'parent.postMessage({ t: "palette" }, location.origin)' not in JS \
        or "}, { capture: true });" not in JS[JS.index('t: "palette" }, location.origin)'):][:120]:
    gaps.add("the pane's capture-phase Ctrl+K relay to the shell")
if '(e.data || {}).t === "palette") palOpen()' not in JS:
    gaps.add("the shell's handler for the pane's {t:\"palette\"} message")
if 'm.t === "focus") paneFocus(m.sid)' not in JS or "paneFocus(window.frameElement?.dataset.focus)" not in JS:
    gaps.add("the pane's {t:\"focus\"} handler and its boot-time fallback for a pane not yet loaded")
if 'id="pal-btn"' not in HTML or 'onclick="palOpen()"' not in HTML:
    gaps.add("the sidebar 'Search or run…' button")
check("Ctrl+K opens the palette from anywhere, even a terminal", gaps,
      "xterm swallows keys, so the pane has to catch Ctrl+K before it and hand it to the shell")

# 16 · Chip trên thanh cửa sổ đưa canvas tới card; tính năng ghim cửa sổ đã bỏ hẳn. Regex chỉ bắt tên
# của tính năng đó: ghim HỘI THOẠI (resume_id, biến `pinned` trong danh sách phiên) dùng chữ khác.
gaps = set(re.findall(r'pinWindow|unpinWindow|applyPin|pinnedNid|rzw|\.pinned\b|"pinned"', JS + HTML))
if not re.search(r'''class="win-chip[^>]*onclick="focusNode\('\$\{esc\(w\.nid\)\}'\)"''', JS):
    gaps.add("the window-bar chip's onclick must be focusNode(w.nid)")
check("window chips jump to the card, window pinning stays gone", gaps,
      "a chip goes through focusNode; no second navigation or pin state")

# 17 · Ô tìm card và Studio đã bỏ hẳn. Sót mảnh nào là hỏng im lặng: một nút bấm không có gì xảy
# ra, hoặc một lời gọi tới route server không còn. Thứ còn lại: focusNode + chip, lối vào trang
# chỉ còn Settings. (?<![-\w]): "story-studio" trong placeholder là tên server ví dụ, không phải tính năng.
pat = (r'cv-find|find-pop|find-row|\b(?:findOpen|findFilter|findClose|findHold|findPick|findKey|findPos'
       r'|renderFindPop)\b|(?<![-\w])studio-(?:btn|view)\b|openPage\([\'"]studio'
       r'|\b(?:img|snd|sed|tts|lib)(?:[A-Z]\w*)\b|id="(?:img|snd|sed|tts|lib)-[\w-]+"'
       r'|/api/(?:images|sounds|tts)|/v1/images|\bi-(?:music|speak|library|image)\b')
gaps = set(re.findall(pat, JS + HTML))
if "window.focusNode = focusNode" not in JS or 'id="win-chips"' not in HTML:
    gaps.add("focusNode and the window-bar chips stay: chips, Show and the palette use them")
if 'const SUBS = { settings: [' not in JS:
    gaps.add("Settings must be the only full-screen page: SUBS is what openPage trusts")
if "if (view && !SUBS[view]) { ACTIVE = -1; shellSave(); }" not in JS:
    gaps.add("a stale ?view=studio or stored shell state must land on Home")
check("the find box and Studio stay gone", gaps,
      "both were removed on purpose; plugins will bring such features back, not leftovers")

# 18 · Double-click tay nắm: setPointerCapture ở pointerdown retarget dblclick về canvas (đã đo trên
# Chrome), nên e.target.closest(".rz") luôn null và tooltip "double-click to reset" thành nói dối.
gaps = set()
dbl = JS[JS.index('cv.addEventListener("dblclick"'):][:300]
if "e.target" in dbl:
    gaps.add("the dblclick handler must not rely on e.target")
if "document.elementFromPoint(e.clientX, e.clientY)" not in dbl:
    gaps.add("the dblclick handler must find the handle with elementFromPoint")
check("double-clicking a resize handle finds the handle under the pointer", gaps,
      "pointer capture sends the dblclick to the canvas")

# 19 · Dismiss gọi route discard, mà có worktree thì discard xoá luôn nhánh: chỉ hàng không có worktree
# mới được có nút này. Hàng đang kiểm lại nằm chung homeInbox: một nguồn cho danh sách, badge, nhãn card.
gaps = set()
row = re.search(r"(.{0,80})<button[^>]*/discard'\)\">Dismiss</button>", JS)
if not row or "!t.worktree" not in row.group(1):
    gaps.add("the Home row's Dismiss must be guarded by !t.worktree")
inbox = JS[JS.index("function homeInbox()"):][:400]
if "t.rechecking" not in inbox:
    gaps.add("homeInbox must keep a row that is being rechecked")
if '"Rechecking sub-agent"' not in JS or '"Reflect failed"' not in JS:
    gaps.add("the row titles Rechecking sub-agent and Reflect failed")
check("Home offers Dismiss only with no worktree, and keeps a row during a recheck", gaps,
      "Discard on a worktree row deletes a branch; a row vanishing mid-check looks solved")

# 20 · Spawn chạy lâu và server chỉ thấy trùng tên sau khi request đầu đăng ký xong: nút còn bấm được
# trong lúc chờ là tạo được hai agent cùng tên.
gaps = set()
spawn = JS[JS.index("async function spawnAgent()"):JS.index("window.spawnAgent = spawnAgent")]
if 'id="sp-submit"' not in HTML:
    gaps.add("the Create agent button needs id=sp-submit")
off, on = spawn.find('$("sp-submit").disabled = true'), spawn.find('finally { $("sp-submit").disabled = false; }')
if off < spawn.find("if (missing) return"):
    gaps.add("spawnAgent must disable sp-submit after the validation return")
if on < off:
    gaps.add("spawnAgent must re-enable sp-submit in finally")
check("Create agent is blocked while a spawn runs", gaps,
      "a second click during the request spawns a duplicate")

# 21 · Đồng hồ context/cache trên card + plan usage ở thanh trên: số liệu tới qua SSE và được vá
# TẠI CHỖ. Mất một mảnh là đồng hồ đứng im mà không lỗi gì — hoặc tệ hơn, mỗi lượt gọi API của run
# nền lại dựng lại cả canvas.
gaps = set()
if 'id="plan-usage"' not in HTML or ".ctx-meter" not in HTML:
    gaps.add("index.html needs #plan-usage and the .ctx-meter style")
if "${meterHtml(s)}" not in JS or 'class="ctx-meter" data-meter=' not in JS:
    gaps.add("agentCard must render the meter span (hidden when there is no data)")
if ('ev.type === "meter") return meterLive(ev)' not in JS
        or 'ev.type === "plan_usage") return renderPlan(ev.plan)' not in JS):
    gaps.add("the SSE handler must apply meter / plan_usage events and return before the refresh")
if "renderPlan(health.plan_usage)" not in JS:
    gaps.add("refresh must render plan usage from /health (reload shows it again)")
check("the context meter and plan usage update live", gaps,
      "the meter freezes, or every API call of a background run rebuilds the canvas")

# 22 · Tab Knowledge: chỉ đọc. Mất một mảnh dây là bấm tab không có gì; một api(..., "POST") lọt vào
# là cái tab "chỉ để xem" bắt đầu đổi dữ liệu.
gaps = set()
if 'id="tab-btn-knowledge"' not in HTML or 'id="tab-knowledge" hidden' not in HTML or 'id="kg-svg"' not in HTML:
    gaps.add("index.html needs #tab-btn-knowledge, a hidden #tab-knowledge and #kg-svg")
if not re.search(r'const TABS = \[[^\]]*"knowledge"', JS):
    gaps.add("TABS must list knowledge")
tabs = JS[JS.index("function switchTab("):JS.index("window.switchTab = switchTab")]
if '$("tab-knowledge").hidden = name !== "knowledge"' not in tabs or 'name === "knowledge") kgLoad()' not in tabs:
    gaps.add("switchTab must show #tab-knowledge and load it")
kgjs = JS[JS.index("const KG_R"):JS.index('window.addEventListener("resize", () => {\n  if (!kg')]
if re.search(r'api\([^)]*,\s*"(POST|PUT|DELETE|PATCH)"|\bact\(|method:', kgjs):
    gaps.add("the Knowledge code must only GET")
if ('kg-looked_up' not in kgjs or 'id="kg-arrow-looked_up"' not in HTML or '.kg-l.kg-looked_up' not in HTML
        or '>Looked up</span>' not in HTML or "Searched, found nothing:" not in kgjs):
    gaps.add("looked_up links need their own class, arrow, legend entry and the agent's gaps on hover")
check("the Knowledge tab is wired and read-only", gaps,
      "the tab button does nothing, or the read-only graph writes data")

# 23 · Tab Board: backlog của một agent. Thiếu picker / một cột / ô thêm là tab mở ra mà không dùng được.
gaps = set()
if not re.search(r'const TABS = \[[^\]]*"board"', JS):
    gaps.add("TABS must list board")
if 'id="tab-btn-board"' not in HTML or 'id="tab-board" hidden' not in HTML:
    gaps.add("index.html needs #tab-btn-board and a hidden #tab-board")
if '$("tab-board").hidden = name !== "board"' not in tabs or 'name === "board") boardLoad()' not in tabs:
    gaps.add("switchTab must show #tab-board and load it")
if 'id="bd-agent"' not in HTML or 'onchange="boardPick(' not in HTML:
    gaps.add("the Board tab needs its agent picker #bd-agent")
if 'id="bd-add"' not in HTML or 'id="bd-title"' not in HTML or 'onsubmit="boardAdd(event)"' not in HTML:
    gaps.add("the Board tab needs the add form (#bd-add, #bd-title)")
for col in ('["todo", "To do"]', '["in_progress", "In progress"]', '["done", "Done"]', '["dropped", "Dropped"]'):
    if col not in JS:
        gaps.add(f"BD_COLS must have the column {col}")
if "if (boardVisible()) boardLoad()" not in JS:
    gaps.add("refresh (SSE) must reload an open Board tab")
check("the Board tab is wired: picker, four columns, add control", gaps,
      "the tab opens empty, a column is missing, or a card cannot be added")

# 24 · Đề xuất của planner chờ NGƯỜI DÙNG: cột Proposed, Approve/Reject gọi /decide, link chỉ http(s)
# (javascript: thành href là XSS), đếm trên nút tab và một mục trên Home.
gaps = set()
cards = JS[JS.index("function bdCard("):JS.index("function boardRender(")]
if '["proposed", "Proposed"]' not in JS or 'k !== "proposed" || hasProposed' not in JS:
    gaps.add("BD_COLS needs a Proposed column, shown only when the agent has proposals")
if ('boardDecideOpen(${c.id}, true)' not in cards or 'boardDecideOpen(${c.id}, false)' not in cards
        or 'id="bd-reason"' not in cards or "/decide`" not in JS[JS.index("function boardDecideGo("):]):
    gaps.add("a proposed card needs Approve and Reject with a reason field that calls /api/board/<id>/decide")
link = JS[JS.index("function bdLink("):JS.index("function bdCard(")]
if ("/^https?:$/.test(x.protocol)" not in link or 'rel="noopener noreferrer"' not in link
        or 'target="_blank"' not in link or "bdLink(c.url)" not in cards or "href=" in cards):
    gaps.add("the source link must come only from bdLink: http(s) only, new tab, noopener noreferrer")
if 'id="board-proposed" class="tab-count"' not in HTML or "bdTabCount(bd.proposed)" not in JS:
    gaps.add("the Board tab button needs a #board-proposed count kept fresh by refresh")
if ("Object.entries(HOME.proposed)" not in JS or "openTeam('${esc(ws)}', 'board')" not in JS
        or 'api("/api/board/proposed")' not in JS):
    gaps.add("Home needs one item per workspace with waiting proposals that opens its Board tab")
check("proposals wait for the user: Proposed column, Approve/Reject, safe link, counts", gaps,
      "a proposal cannot be decided, a javascript: url becomes a link, or nobody sees it waiting")
# 23b · Planner của agent đang chọn: panel riêng #bd-planner ngay sau .team-head của tab Board.
gaps = set()
bd = HTML[HTML.index('<div id="tab-board" hidden>'):HTML.index('<div id="bd-cols"')]
if 'id="bd-planner"' not in bd or bd.index('class="team-head"') > bd.index('id="bd-planner"'):
    gaps.add("#bd-planner must sit inside #tab-board, right after .team-head")
for needle, what in (('id="pl-on" role="switch" onchange="plannerSave()"', "the on/off switch"),
                     ('id="pl-brief" maxlength="4000"', "the brief"),
                     ('id="pl-every" class="mini" type="number" min="1" max="90"', "the interval"),
                     ('onsubmit="plannerSave(event)"', "Save"),
                     ('id="pl-scout" class="secondary" type="button" onclick="plannerScout()"', "Scout now"),
                     ('it cannot read your files or act.', "the hint line")):
    if needle not in bd:
        gaps.add(f"#bd-planner needs {what}")
if "plannerLoad().catch(" not in JS[JS.index("async function boardLoad("):JS.index("function bdCard(")]:
    gaps.add("boardLoad must load the planner panel")
if '"Scouting…" : "Scout now"' not in JS:
    gaps.add("Scout now must show a running state")
check("the Board tab has the planner panel: switch, brief, interval, Save, Scout now", gaps,
      "the planner cannot be switched on, scheduled or run from the dashboard")

# Luật một-người-ghi ở SERVER (_WRITERS). Khoá cũ của trình duyệt đóng WS terminal giữa lượt người
# dùng đang gõ khi thấy signal/run, và khoá tới khi bấm 🔄 — chỉ chạy khi card đang hiện trên màn hình.
gaps = set()
card = JS[JS.index("function agentCard("):JS.index("function quickBtns(")]
if re.search(r"termLock|data-lock|\.ws\.close\(", card) or "termLock" in JS:
    gaps.add("agentCard still closes the terminal websocket or keeps a termLock")
if 'e.code === 4409' not in JS or "connectTerm(t)" not in JS[JS.index("e.code === 4409"):][:400]:
    gaps.add("startTerm must wait on the server's 4409 (busy) and reconnect by itself")
if "s.term_queue" not in card or "waiting" not in card:
    gaps.add("the card must show how many messages wait for the open terminal")
check("the terminal is never killed by the browser for a signal or a run", gaps,
      "the single-writer rule lives on the server")

# Đội chạy cố định Claude Opus 5.5 (team.TEAM_MODEL): picker model theo vai mà còn là người dùng
# chọn Gemini, server lặng lẽ bỏ qua, và /api/team/models đã không còn.
gaps = set()
for needle in ("team-models", "teamModel", "TEAM_ROLES", "TEAM.models", "/api/team/models", "Models per role"):
    if needle in JS or needle in HTML:
        gaps.add(f"{needle} is still in the dashboard")
if 'id="trs-models"' not in HTML or "TEAM.learned_effort" not in JS or "s.effort_line" not in JS:
    gaps.add("the Team tab must show the fixed model with the learned start, and each card its effort")
check("the Team tab has no per-role model pickers", gaps,
      "sub-agents always run Claude Opus 5.5; only effort varies")

if fails:
    print("\n" + "\n\n".join(fails))
    sys.exit(1)
print("\nall checks passed")
