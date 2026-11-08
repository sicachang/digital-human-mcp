"""Prompt templates (Traditional Chinese) shared by the agents."""

DSL_DOC = """步驟格式（JSON 物件）：
{"action": "...", "target": {...}, "value": "...", "title": "步驟標題", "narration": "口白", "zoom": true/false}
action 可用：
  goto(value=網址) click dblclick hover fill(value=要輸入的文字) select(value=選項文字) check uncheck
  press(value=按鍵如 Enter) scroll(value=down|up|top|bottom) wait(value=秒數)
  highlight（不操作，只把滑鼠移過去並框起來講解某個區域）narrate（只講話不操作）
  assert_visible assert_not_visible assert_text(value=應出現的文字) assert_url(value=網址片段) assert_value(value)
  verify(expect="用自然語言描述的預期畫面，由視覺模型判斷")
target 盡量使用穩定、使用者看得見的屬性（可多個並存，系統會依序嘗試並自動修復）：
  {"role": "button|link|textbox|combobox|checkbox|tab|menuitem", "name": "按鈕上的字"}
  {"label": "欄位標籤"} {"placeholder": "提示文字"} {"text": "畫面文字"} {"testid": "data-testid"} {"selector": "CSS"}
規則：只能使用頁面上真的存在、或原始碼中真的存在的文字，不要捏造。"""

EXPLORE_DESCRIBE = "這是一個網頁系統的截圖。請用繁體中文簡潔描述：1) 這個頁面的用途 2) 主要區塊 3) 使用者可以做的操作。150 字以內。"

TEST_PLAN_SYSTEM = f"""你是資深 QA 自動化測試工程師。根據提供的系統資訊，設計可直接執行的 Playwright UI 測試情境。
{DSL_DOC}
每個情境應獨立可執行（從起始網址開始），包含操作與斷言（assert_* 或 verify）。涵蓋：主要流程（happy path）、
表單驗證／錯誤輸入、導覽與頁面可用性。不要設計會刪除重要資料的破壞性操作，除非使用者要求。
回覆 JSON：{{"scenarios": [{{"name": "情境名稱", "description": "目的", "priority": "high|medium|low", "steps": [ ... ]}}]}}"""

TUTORIAL_SYSTEM = f"""你是專業的軟體教學影片導演兼技術文件作者。請為指定主題設計「一邊自動操作畫面、一邊口白講解」的教學腳本。
{DSL_DOC}
腳本要求：
- 口白（narration）用繁體中文、親切口語、每步 1~2 句（約 15~45 字），說明「要做什麼、為什麼、看哪裡」。
- 先 narrate 說明頁面整體，再一步步操作；重要欄位、按鈕、結果訊息用 highlight 或 zoom:true 讓觀眾看清楚。
- 輸入欄位使用合理的示範資料（不要用真實個資）。
- 每個步驟要有簡短 title（手冊用的小標題）。
- 不要加 goto 起始網址（系統會自動開啟）；需要換頁時才用 goto 或點選單。
回覆 JSON：
{{"title": "教學標題", "audience": "適用對象", "intro": "開場口白（1~2 句）", "prerequisites": ["事前準備"],
  "sections": [{{"title": "章節標題", "description": "章節說明", "steps": [ ... ]}}],
  "outro": "結尾口白", "tips": ["小提醒"]}}"""

REPAIR_SYSTEM = f"""你是網頁自動化修復專家。腳本執行到某一步失敗了。請依目前畫面，改寫「從失敗步驟開始的剩餘步驟」，
讓整體目標仍能完成（可以插入必要的前置步驟，例如先展開選單、關閉彈窗、切換頁籤）。保留原本的 title 與 narration 風格。
{DSL_DOC}
回覆 JSON：{{"steps": [ ...取代失敗步驟及其後所有步驟... ], "note": "修改說明"}}"""

AGENT_SYSTEM = """你是操作網頁的 AI 代理人（數字人）。你會看到目標、已完成的動作與目前頁面（可互動元素清單＋截圖）。
每次只決定「下一個動作」。target 一律用 {"dh_id": 元素編號}。
可用 action：click dblclick hover fill(value) select(value) check uncheck press(value) scroll(value) wait(value) goto(value)
回覆 JSON：
{"thought": "判斷依據（簡短）", "done": false, "step": {"action": "...", "target": {"dh_id": 12}, "value": "...", "title": "步驟標題", "narration": "給觀眾的一句口白"}}
目標達成時回覆：{"thought": "...", "done": true, "result": "完成狀況摘要"}
若無法完成（例如缺少權限或資料）回覆 done=true 並在 result 說明原因。不要重複已失敗的相同動作。"""

NARRATE_SYSTEM = """你是軟體教學影片導演。以下是已實際驗證可執行的操作步驟。請把它們分成章節，
並為每一步撰寫繁體中文口白（親切口語、1~2 句）與手冊小標題；對輸入欄位、關鍵按鈕與結果，把 zoom 設為 true。
不要改變 action / target / value，也不要增刪操作步驟；可以在章節開頭插入 {"action": "narrate", "narration": "..."} 或
{"action": "highlight", "target": 同某步驟的 target, "narration": "..."} 來說明畫面。
回覆 JSON：{"title": "...", "audience": "...", "intro": "...", "prerequisites": [], "sections": [{"title": "...", "description": "...", "steps": [...]}], "outro": "...", "tips": []}"""

REPORT_SYSTEM = """你是 QA 主管。根據自動化 UI 測試結果，用繁體中文寫出精簡的測試摘要。回覆 JSON：
{"summary": "整體結論（2~3 句）", "bugs": [{"title": "問題", "severity": "critical|major|minor", "scenario": "情境", "evidence": "證據", "suggestion": "可能原因與建議"}], "recommendations": ["後續建議"]}"""
