# 智慧數字人 MCP（Digital Human）

完全離線運作的 MCP 服務，接上公司內網的 Qwen 模型（OpenAI 相容 API），讓 **Qwen Code CLI** 或 **WeKnora** 可以：

| 能力 | 說明 |
|---|---|
| 🧪 自動 UI 測試 | AI 依網址、目標、專案原始碼規劃測試情境 → Playwright 執行 → HTML 報告（每步截圖＋錄影）＋AI 問題摘要＋可放進 CI 的 `pytest` 檔 |
| 🎬 教學影片 | **燒錄字幕＋離線中文語音口白**、**看得見的滑鼠平滑移動＋點擊波紋**、**AI 逐字打字**、**局部放大運鏡**、開場標題卡 → `tutorial.mp4`（另附 `.srt`） |
| 📘 教學手冊 | 每一步都有標註紅框的截圖 → `manual.html / .pdf / .docx / .md` |
| 🧠 讀懂程式碼 | 掃描路由、頁面、表單標籤、按鈕文字、`data-testid`、i18n，整理成功能地圖，依「真實存在的 UI」設計腳本 |
| 🤖 自我修復 | 找不到元素時，視覺模型看截圖＋元素清單自動改定位；排練失敗時 AI 改寫剩餘步驟；仍然失敗時改由代理人實地摸索流程 |

## 架構（多代理流程）

```
            ┌────────── 專案原始碼分析（功能地圖） ──────────┐
使用者目標 → │ 規劃者 Planner：設計測試情境／教學腳本（口白、放大重點） │
            └──────────────────────┬────────────────────┘
                                   ▼
            執行者 Executor（Playwright）── 找不到元素 → 修復者 Healer（視覺模型＋元素清單）
                                   │                  └→ 改寫剩餘步驟 Repair／代理人 Agent 實地摸索
                                   ▼
            驗證者 Verifier（斷言＋視覺模型判斷「預期畫面」）
                                   ▼
            錄製者 Recorder：滑鼠動畫、逐字打字、口白同步、紅框標註截圖
                                   ▼
            後製 Post：ffmpeg 放大運鏡 → 字幕 → 混音 → mp4 ；手冊 HTML/PDF/DOCX
```

教學影片的完整流程：**設計腳本 → 排練（自動修正定位與步驟）→ 預先合成全部語音 → 正式錄影（依語音長度控制節奏，先講解再點擊）→ 後製 → 產出手冊**。

---

## 一、在有網路的電腦上打包（只做一次）

```bash
./scripts/build_offline.sh
```
產生 `dist/digital-human-mcp-1.0.0.tar.gz`（映像檔）與 `dist/digital-human-deploy-1.0.0.tar.gz`（部署檔）。
映像檔內已包含 Chromium、ffmpeg、中文字型（Noto CJK）、Piper 中文語音模型與所有 Python 套件，**執行時完全不需要網路**。

> 公司網路有 TLS 攔截代理時：`CA_CERT=/path/corp-ca.crt ./scripts/build_offline.sh`

## 二、在公司內網部署

```bash
tar xzf digital-human-deploy-1.0.0.tar.gz
./scripts/load_offline.sh digital-human-mcp-1.0.0.tar.gz   # docker load + compose up
```

接著編輯 `.env`：

- `LLM_BASE_URL=http://172.28.26.96:9192/v1`（預設值就是這個）
- `PUBLIC_BASE_URL=http://<這台主機IP>:8000`：讓工具回傳可直接點開的影片、手冊連結
- `PROJECTS_HOST_DIR=/srv/code`：放專案原始碼的資料夾（唯讀掛載到容器的 `/workspace`）

改完 `.env` 後執行 `docker compose up -d`。

不用 compose 也可以直接執行：

```bash
docker run -d --name digital-human -p 8000:8000 --shm-size=1g \
  -e LLM_BASE_URL=http://172.28.26.96:9192/v1 \
  -e PUBLIC_BASE_URL=http://10.0.0.5:8000 \
  -v $PWD/outputs:/data/outputs \
  -v /srv/code:/workspace:ro \
  digital-human-mcp:1.0.0
```

| 端點 | 用途 |
|---|---|
| `http://主機:8000/mcp` | MCP Streamable HTTP（Qwen Code 建議用這個） |
| `http://主機:8000/sse` | MCP SSE（WeKnora 或舊版客戶端） |
| `http://主機:8000/` | 成果首頁（影片、手冊、報告連結） |
| `http://主機:8000/files/...` | 所有產出檔案 |

## 三、接上 Qwen Code CLI

編輯 `~/.qwen/settings.json`（或專案內的 `.qwen/settings.json`），範本在 `configs/`：

```json
{
  "mcpServers": {
    "digital-human": { "httpUrl": "http://10.0.0.5:8000/mcp", "timeout": 900000, "trust": true }
  }
}
```

- 想在本機以 stdio 直接啟動容器，請參考 `configs/qwen-code.settings.stdio.json`。
- 把 `configs/QWEN.md.example` 複製到你專案的 `QWEN.md`，寫上平台網址、測試帳號與專案路徑。之後在 Qwen Code 裡只要說「幫我測試客戶管理頁」或「幫我做一支新增訂單的教學影片」，它就知道要怎麼做。

對話範例：
```
> 用 dh_check_environment 檢查服務
> 登入 http://10.1.2.3:8080/login 帳號 demo 密碼 demo123，設定檔叫 dev
> 讀懂 my-platform 專案後，幫我測試訂單模組並給我報告
> 幫我做一支「如何新增訂單並送審」的教學影片和手冊，對象是新進業務
```

## 四、接上 WeKnora

在 WeKnora 的 MCP 服務設定裡新增一個服務：

- 傳輸方式選 **SSE**，URL 填 `http://<主機IP>:8000/sse`
- 或選 **Streamable HTTP**，URL 填 `http://<主機IP>:8000/mcp`

若 WeKnora 跟本服務放在同一台 Docker 主機，可以讓兩者加入同一個 docker network，URL 改用 `http://digital-human-mcp:8000/sse`。

## 五、工具一覽

| 工具 | 用途 |
|---|---|
| `dh_check_environment` | 檢查 LLM、瀏覽器、ffmpeg、語音引擎 |
| `dh_login` | 登入並存成登入設定檔 `auth_profile`，其他工具共用 |
| `dh_analyze_project` / `dh_read_project_files` | 讀懂原始碼，整理成功能地圖 |
| `dh_explore_site` | 爬網站，產出截圖、元素清單與視覺描述 |
| `dh_plan_ui_tests` → `dh_run_ui_tests` | 規劃並執行測試（只給 `goal` 時會一鍵自動規劃） |
| `dh_agent_task` | 數字人看畫面自己操作，完成目標後回傳可重播的步驟 |
| `dh_design_tutorial` → `dh_record_tutorial` | 先產生腳本讓你審稿，再錄製 |
| `dh_create_tutorial` | 一鍵完成：設計 → 排練修正 → 錄影 → 後製 → 手冊（背景執行） |
| `dh_job_status` / `dh_list_jobs` / `dh_list_outputs` | 查詢背景任務與成果 |

### 步驟格式（測試和教學腳本共用，可以手動修改）

```json
{"action": "fill", "target": {"label": "客戶名稱"}, "value": "台灣科技", "title": "輸入客戶名稱",
 "narration": "輸入客戶名稱，這是必填欄位。", "zoom": true}
```

- **action**：`goto` `click` `dblclick` `hover` `fill` `select` `check` `uncheck` `press` `scroll` `wait` `highlight`（框起來講解）`narrate`（只講話）`assert_text` `assert_visible` `assert_not_visible` `assert_url` `assert_value` `verify`（用自然語言描述預期，由視覺模型判斷）
- **target**：`role`+`name`、`label`、`placeholder`、`text`、`testid`、`selector`。可以多個並存，系統會依序嘗試，都找不到時交給 AI 修復。
- **zoom**：`true` 表示放大這一步的區域。輸入欄位、下拉選單和 `highlight` 步驟預設會自動放大。

## 六、語音口白

| `TTS_ENGINE` | 說明 |
|---|---|
| `piper`（預設） | 內建離線神經網路中文語音（普通話）。字幕維持繁體，語音合成前會自動轉成簡體以提高發音準確度 |
| `openai` | 公司若有自架 TTS（CosyVoice、GPT-SoVITS 等 OpenAI 相容 `/audio/speech` 端點），設定 `TTS_BASE_URL` 即可換成更自然的聲音 |
| `espeak` | 備援，聲音較機械 |
| `none` | 只有字幕，沒有口白 |

語速用 `TTS_LENGTH_SCALE` 調整（1.2 較慢、0.9 較快）。

## 七、注意事項

- 教學製作預設會先**排練**一次。排練會真的執行操作，例如新增一筆資料，所以請在測試環境使用，或傳入 `rehearse=false`。
- 平台網址如果是公司內部 DNS 名稱、容器解析不到，請在 compose 的 `extra_hosts` 補上。
- 長任務（教學錄製）會在背景執行，再用 `dh_job_status` 輪詢；Qwen Code 的 `timeout` 建議設 900000（15 分鐘）。
- 自我測試：`demo/` 有示範 CRM，`tests/mock_llm.py` 是模擬 LLM，`tests/smoke_test.py` 是端對端測試。
