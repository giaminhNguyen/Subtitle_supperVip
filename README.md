# YouTube Subtitle Manager

Ứng dụng web quản lý subtitle hàng loạt từ nhiều kênh YouTube. Dán URL kênh, quét toàn bộ video công khai, tải caption theo hàng đợi bền vững, theo dõi chi tiết trạng thái và lưu subtitle có tổ chức trên máy.

> Caption được lấy bằng [`youtube-transcript-api`](https://github.com/jdepoix/youtube-transcript-api). Ứng dụng không dùng Selenium, ChromeDriver hay trình duyệt headless để tải subtitle.

## Mục lục

- [Tính năng](#tính-năng)
- [Kiến trúc](#kiến-trúc)
- [Yêu cầu hệ thống](#yêu-cầu-hệ-thống)
- [Cài đặt bằng Docker](#cài-đặt-bằng-docker)
- [Chạy local trên Windows](#chạy-local-trên-windows)
- [Tạo YouTube API key](#tạo-youtube-api-key)
- [Cách sử dụng](#cách-sử-dụng)
- [Lưu trữ subtitle](#lưu-trữ-subtitle)
- [Trạng thái xử lý](#trạng-thái-xử-lý)
- [REST API](#rest-api)
- [Kiểm thử](#kiểm-thử)
- [Xử lý lỗi thường gặp](#xử-lý-lỗi-thường-gặp)
- [Giới hạn](#giới-hạn)

## Tính năng

- Thêm nhiều kênh từ `@handle`, `/channel/CHANNEL_ID`, `/user/...` và custom URL.
- Quét bằng **Uploads Playlist** của YouTube Data API v3 để vượt giới hạn 500 kết quả của `search.list`.
- Lưu metadata, nhận diện video thường / Short / live đã lưu và tránh tạo video trùng.
- Cấu hình từng kênh: ưu tiên ngôn ngữ (ví dụ `vi → en → original`), sub thủ công/tự động/bất kỳ, dùng dịch YouTube, định dạng xuất.
- Xuất `SRT`, `VTT`, `TXT`, `JSON`, `CSV` và `metadata.json`.
- Hàng đợi SQLite bền vững: pause, resume, cancel, retry, exponential backoff và khôi phục job sau restart.
- Đồng bộ video mới theo lịch riêng từng kênh.
- Dashboard, danh sách video theo trạng thái, cấu hình kênh, jobs và logs.

## Kiến trúc

```text
┌──────────────────────┐        REST API        ┌────────────────────────┐
│ React + TypeScript   │ ──────────────────────► │ FastAPI                │
│ Vite admin UI        │ ◄────────────────────── │ SQLAlchemy + SQLite    │
└──────────────────────┘                         └───────────┬────────────┘
                                                              │
                         ┌────────────────────────────────────┼───────────────────────────┐
                         ▼                                    ▼                           ▼
                YouTube Data API v3                   Persistent worker              data/ files
                channel + uploads playlist             scan / download / retry       subtitle + metadata
```

| Thành phần | Vai trò |
| --- | --- |
| `backend/app/main.py` | FastAPI REST API và validation |
| `backend/app/worker.py` | Worker queue riêng, retry và recovery |
| `backend/app/services/youtube.py` | Resolve URL và quét Uploads Playlist |
| `backend/app/services/subtitles.py` | Chọn track, lấy transcript, export file |
| `backend/alembic/` | Database migrations |
| `frontend/` | React + TypeScript + Vite admin UI |

## Yêu cầu hệ thống

Chọn một cách chạy:

| Cách chạy | Cần cài |
| --- | --- |
| Docker | Docker Desktop 4+ (Docker Compose) |
| Local Windows | Python **3.12+**, Node.js **22+**, npm |

Bạn cũng cần API key của **YouTube Data API v3**. Không cần Selenium hay browser automation.

## Cài đặt bằng Docker

### 1. Cấu hình môi trường

```powershell
cd E:\OTHER\Subtitle_supperVip
Copy-Item .env.example .env
notepad .env
```

Thay giá trị sau bằng API key thật:

```env
YOUTUBE_API_KEY=YOUR_GOOGLE_YOUTUBE_DATA_API_KEY
```

### 2. Khởi động

```powershell
docker compose up --build
```

Khi hoàn tất, mở:

- UI: <http://localhost:5173>
- Swagger API docs: <http://localhost:8000/docs>
- Health check: <http://localhost:8000/health>

Chạy nền:

```powershell
docker compose up -d --build
```

Theo dõi worker:

```powershell
docker compose logs -f worker
```

## Chạy local trên Windows

Mở ba cửa sổ PowerShell.

### 1. Tạo `.env`

```powershell
cd E:\OTHER\Subtitle_supperVip
Copy-Item .env.example .env
notepad .env
```

Điền `YOUTUBE_API_KEY`, lưu file.

### 2. Chạy backend API

```powershell
cd E:\OTHER\Subtitle_supperVip\backend

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
alembic upgrade head

uvicorn app.main:app --reload --port 8000
```

Nếu PowerShell chặn `Activate.ps1`, áp dụng cho cửa sổ hiện tại rồi chạy lại lệnh activate:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### 3. Chạy worker

```powershell
cd E:\OTHER\Subtitle_supperVip\backend
.\.venv\Scripts\Activate.ps1
python -m app.worker
```

> API chỉ tạo job. Worker là tiến trình thực thi quét/tải subtitle, vì vậy cần giữ cửa sổ này chạy.

### 4. Chạy frontend

```powershell
cd E:\OTHER\Subtitle_supperVip\frontend
npm install
npm run dev
```

Mở <http://localhost:5173>.

## Tạo YouTube API key

1. Vào [Google Cloud Console](https://console.cloud.google.com/), tạo hoặc chọn Project.
2. Vào **APIs & Services → Library** và bật **YouTube Data API v3**.
3. Vào **APIs & Services → Credentials → Create credentials → API key**.
4. Dán API key vào `YOUTUBE_API_KEY` trong `.env`.
5. Khuyến nghị giới hạn API key theo API/app trong Google Cloud Console trước khi dùng thực tế.

Không commit `.env`, API key, cookie hoặc proxy credential. `.gitignore` đã bỏ qua `.env` và dữ liệu sinh ra.

## Cách sử dụng

1. Mở dashboard, dán URL một kênh YouTube rồi bấm **Thêm kênh**.
2. Vào chi tiết kênh, cấu hình ngôn ngữ, loại subtitle, dịch tự động, format xuất và chu kỳ đồng bộ.
3. Chọn một thao tác:
   - **Quét toàn bộ**: đọc toàn bộ Uploads Playlist, chỉ tải video chưa hoàn tất.
   - **Đồng bộ video mới**: chỉ thêm video mới/chưa xử lý.
   - **Xử lý lỗi / thiếu sub**: đưa video lỗi hoặc thiếu subtitle vào queue lại.
4. Xem tiến độ ở bảng video hoặc **Jobs & logs**.
5. File hoàn tất nằm trong `data/`.

## Lưu trữ subtitle

```text
data/
└── ten-kenh/
    └── 2026-09-ten-video-video_id/
        ├── vi.srt
        ├── vi.txt
        ├── vi.json
        └── metadata.json
```

Tên file được làm sạch để tương thích Windows, macOS và Linux. `metadata.json` gồm metadata video, track subtitle đã chọn, nguồn manual/auto, thông tin dịch và danh sách file xuất.

## Trạng thái xử lý

| Trạng thái | Ý nghĩa |
| --- | --- |
| `pending` | Chưa đưa vào xử lý |
| `queued` | Đang chờ worker |
| `processing` | Worker đang xử lý |
| `completed` | Xuất subtitle thành công |
| `no_subtitle` | Không có transcript/caption |
| `language_unavailable` | Có sub nhưng không có ngôn ngữ phù hợp |
| `failed` | Hết retry hoặc lỗi không khôi phục được |
| `blocked` | YouTube rate-limit/chặn IP |
| `skipped` | Người dùng bỏ qua |

Lỗi mạng tạm thời sẽ retry theo exponential backoff. Job `processing` khi ứng dụng dừng sẽ được đưa trở lại queue khi worker khởi động lần sau.

## REST API

Mở Swagger UI tại <http://localhost:8000/docs>.

| Method | Endpoint | Mục đích |
| --- | --- | --- |
| `POST` | `/api/channels` | Thêm kênh bằng URL |
| `GET` | `/api/channels` | Danh sách kênh |
| `PUT` | `/api/channels/{id}/settings` | Lưu cấu hình subtitle/lịch sync |
| `POST` | `/api/channels/{id}/scan` | Queue scan `all`, `new`, `since`, `retryable` |
| `POST` | `/api/channels/{id}/sync` | Đồng bộ video mới |
| `GET` | `/api/channels/{id}/videos` | Video có filter status/title/language |
| `GET` | `/api/videos/{id}/available-subtitles` | Kiểm tra tracks hiện có |
| `POST` | `/api/videos/{id}/download?force=true` | Tải lại subtitle video |
| `GET` | `/api/jobs`, `/api/logs` | Queue và nhật ký |
| `POST` | `/api/jobs/{id}/pause`, `/resume`, `/cancel`, `/retry` | Điều khiển job |

Ví dụ thêm kênh bằng PowerShell:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/api/channels `
  -ContentType 'application/json' `
  -Body '{"url":"https://www.youtube.com/@GoogleDevelopers"}'
```

## Kiểm thử

Sau khi cài backend dependencies:

```powershell
cd E:\OTHER\Subtitle_supperVip\backend
.\.venv\Scripts\Activate.ps1
pytest -q
```

Test bao phủ parse URL, chống trùng video/job, chọn subtitle, serializer SRT và retry/backoff.

Build frontend:

```powershell
cd E:\OTHER\Subtitle_supperVip\frontend
npm run build
```

## Xử lý lỗi thường gặp

### Không tìm thấy `py`/Python

Cài Python 3.12+ từ [python.org](https://www.python.org/downloads/) và chọn **Add Python to PATH**. Mở PowerShell mới rồi kiểm tra:

```powershell
py -3.12 --version
```

### `YOUTUBE_API_KEY` chưa cấu hình

Đảm bảo `.env` nằm ở thư mục gốc dự án, không phải trong `backend/`:

```powershell
Get-Content E:\OTHER\Subtitle_supperVip\.env
```

### API trả quota exceeded / 403

Kiểm tra API đã bật, key thuộc đúng Google Cloud Project và còn quota. Kênh lớn dùng quota đáng kể vì ứng dụng lấy video details theo từng trang playlist.

### `no_subtitle`

Video có thể không có caption, bị xóa/private, giới hạn tuổi, hoặc tác giả đã tắt transcript. Đây không nhất thiết là lỗi ứng dụng.

### `blocked`

YouTube có thể rate-limit/chặn IP khi tải dồn dập. Giảm `REQUESTS_PER_MINUTE`, chờ rồi retry. Không tăng concurrency một cách thiếu kiểm soát.

### Không thấy file subtitle

Đảm bảo worker còn chạy: local dùng cửa sổ `python -m app.worker`; Docker dùng `docker compose logs -f worker`. Xem chi tiết trong Jobs & logs.

## Giới hạn

- Chỉ quét video công khai mà YouTube Data API trả về.
- Không thể đảm bảo transcript cho private/removed/age-restricted video hoặc video không có caption.
- YouTube không đảm bảo endpoint transcript công khai ổn định; hãy giữ tốc độ request thấp.
- YouTube API key chỉ dùng cho metadata/danh sách video; transcript lấy qua `youtube-transcript-api`.
- SQLite phù hợp MVP/một worker. Hệ thống nhiều worker/người dùng nên chuyển `DATABASE_URL` sang PostgreSQL và dùng queue broker chuyên dụng.

## Biến môi trường

| Biến | Mặc định | Mô tả |
| --- | --- | --- |
| `YOUTUBE_API_KEY` | — | Bắt buộc để resolve/quét kênh |
| `DATABASE_URL` | `sqlite:///./data/app.db` | SQLAlchemy database URL |
| `DATA_DIR` | `./data` | Nơi lưu subtitle và metadata |
| `REQUESTS_PER_MINUTE` | `20` | Tốc độ request tối đa khuyến nghị |
| `WORKER_POLL_SECONDS` | `2` | Chu kỳ kiểm tra queue của worker |
| `CORS_ORIGINS` | `http://localhost:5173` | Origin frontend được phép gọi API |
| `VITE_API_BASE_URL` | `http://localhost:8000/api` | API URL phía frontend |

---

Toàn bộ subtitle được lưu tại máy của bạn trong `data/`.
