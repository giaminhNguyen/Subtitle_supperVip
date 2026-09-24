# YouTube Subtitle Manager

Ứng dụng quản lý subtitle hàng loạt từ các kênh YouTube. Dán URL kênh, quét video công khai, đưa việc tải caption vào hàng đợi và lưu subtitle có tổ chức trên máy.

## Chạy nhanh trên Windows

Yêu cầu duy nhất cần cài trước:

- Python 3.12 trở lên (bản khóa được sinh trên Python 3.13), chọn **Add Python to PATH** khi cài.
- Node.js 22 trở lên.

Sau khi clone hoặc giải nén dự án, chỉ cần bấm đúp:

```text
start-app.bat
```

Launcher tự động tạo Python virtual environment, cài dependency còn thiếu, tạo/cập nhật SQLite, khởi động API, worker và web ở nền, rồi mở <http://localhost:5173>.

Để dừng ứng dụng, bấm:

```text
stop-app.bat
```

## Cấu hình YouTube API key

1. Mở <https://console.cloud.google.com/> và chọn hoặc tạo project.
2. Vào **APIs & Services → Library**, tìm và bật **YouTube Data API v3**.
3. Vào **APIs & Services → Credentials → Create Credentials → API key**.
4. Sau khi chạy ứng dụng, dán key vào phần **Cấu hình YouTube API key** trên trang chủ.

Key được lưu trong database SQLite cục bộ (`data/app.db`, dùng chung giữa API và worker, kể cả Docker); ứng dụng không trả key về web sau khi đã lưu. Nếu bạn đã có `YOUTUBE_API_KEY` trong `.env`, key đó được tự nhập vào database ở lần dùng đầu tiên (DB thắng nếu đã có). `.env` và `data/` đã được bỏ qua bởi Git.

Hoặc nhập key qua terminal mà không hiện ký tự đã gõ:

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.cli set-youtube-api-key
```

Không truyền key trực tiếp trên command line vì có thể bị lưu vào lịch sử terminal.

## Sử dụng

1. Mở <http://localhost:5173>.
2. Dán URL kênh, ví dụ `https://www.youtube.com/@phuthuyaudioso`.
3. Chọn kênh vừa thêm và bấm **Quét toàn bộ**.
4. Worker tải subtitle theo cấu hình ngôn ngữ/định dạng của kênh.
5. Theo dõi tiến trình tại **Jobs & logs**.

Ứng dụng nhận các dạng URL `@handle`, `/channel/ID`, `/user/...` và custom URL. Chỉ video công khai được YouTube Data API trả về mới được quét.

### Giao diện

- **Tự làm mới**: dashboard, danh sách video và Jobs & logs tự cập nhật (nhanh hơn khi có job đang chạy, chậm khi rảnh) và tạm dừng khi tab bị ẩn; không bao giờ gửi chồng request.
- **Phân trang**: video, job và log dùng `limit`/`offset` (tối đa 500/lần, giao diện dùng 50); tổng số nằm trong header `X-Total-Count`.
- **Chẩn đoán**: bấm nhãn trạng thái ở đầu trang (hoặc mở trang Chẩn đoán) để xem worker, database, dung lượng, hàng đợi và SyncRun đang chạy.
- **Retry job lỗi** ngay trong Jobs & logs; API từ chối (409) nếu đã có job đang hoạt động cho cùng video/kênh.
- Chạy kiểm thử giao diện: `cd frontend && npm test`.

### Đồng bộ kênh

- **Sync new** (và lịch tự động) chỉ quét các video mới nhất: duyệt playlist uploads từ mới đến cũ và dừng khi gặp lại video mốc đã lưu cộng thêm một dải video đã biết (`SYNC_SAFETY_WINDOW`, mặc định 20). Lần đồng bộ đầu tiên của kênh (hoặc kênh cũ chưa có mốc, hoặc mốc không còn trong playlist) sẽ tự quét toàn bộ lịch sử một lần rồi mới dùng chế độ nhanh.
- **Quét toàn bộ** luôn duyệt hết playlist, không dừng sớm; dùng nó nếu nghi ngờ bị sót video.
- `uploads_playlist_id` được lưu sau lần resolve đầu tiên; nếu YouTube báo playlist không còn tồn tại, ứng dụng resolve lại đúng một lần.
- Mỗi lần đồng bộ là một *SyncRun* gồm job quét và mọi job tải do nó tạo ra. Run chỉ kết thúc (`completed`/`partial`/`failed`) khi tất cả job con đã ở trạng thái cuối; số liệu luôn được tính lại từ trạng thái job nên retry/khởi động lại không làm đếm trùng.

## Sức khỏe, backup và khôi phục

- `GET /health` (nhẹ, không gọi YouTube): `status` là `ok`, `degraded` (worker offline/chưa chạy hoặc chưa có API key; vẫn HTTP 200) hoặc `critical` (database/thư mục dữ liệu lỗi; HTTP 503). Worker ghi heartbeat vào SQLite mỗi `WORKER_HEARTBEAT_SECONDS` (10s); quá `WORKER_STALE_SECONDS` (45s) là offline. Worker treo không ghi heartbeat nên cũng bị coi là offline.
- `GET /api/diagnostics`: phiên bản, revision Alembic, journal mode, kích thước DB, số job theo trạng thái, worker, SyncRun đang chạy, dung lượng ổ đĩa và lần sync thành công gần nhất. Không trả API key hay đường dẫn tuyệt đối.
- **Backup tự động**: `start-app.bat` dừng tiến trình cũ, rồi nếu có migration sắp chạy thì sao lưu database vào `.runtime\backups\subtitle-db-YYYYMMDD-HHMMSS.sqlite3` (bằng SQLite backup API, an toàn cả khi DB đang mở) **trước** khi migrate. Backup lỗi thì không migrate. Giữ `DB_BACKUP_KEEP_COUNT` (10) bản gần nhất. Trong Docker, backup nằm ở `data/backups`.
- **Khôi phục**: dừng ứng dụng (`stop-app.bat`), chạy `restore-db.bat`, chọn bản backup. Bản được kiểm tra trước, database hiện tại được lưu thành `subtitle-db-prerestore-*` rồi mới thay thế; backup đã chọn không bị xóa. Sau đó chạy lại `start-app.bat`.
- **Log runtime** `.runtime\logs`: log của lần chạy trước được lưu lại kèm timestamp và xóa sau `RUNTIME_LOG_RETENTION_DAYS` (14) ngày.
- **Dọn lịch sử DB** (worker chạy lúc khởi động rồi mỗi `MAINTENANCE_INTERVAL_HOURS`=6 giờ): xóa job/SyncRun đã kết thúc cũ hơn `JOB_HISTORY_RETENTION_DAYS` (30), log job cũ hơn `JOB_LOG_RETENTION_DAYS` (14) và bản ghi heartbeat chết quá 7 ngày. Không đụng tới channel/video/subtitle hay job đang hoạt động.
- Scan tự động thất bại hẳn sẽ được hoãn `SCAN_FAILURE_RETRY_MINUTES` (30) phút thay vì xếp lại ngay.
- **Worker offline?** Xem `/health`; kiểm tra `.runtime\logs\worker.err.log` và chạy lại `start-app.bat`.

## Cấu trúc dữ liệu portable

Mọi dữ liệu runtime đều nằm trong thư mục dự án, vì vậy có thể di chuyển hoặc clone dự án đến vị trí khác:

```text
.env                 # API key và cấu hình local, không commit
data/                # SQLite database, subtitle và metadata
.runtime/logs/       # Log của launcher
backend/.venv/       # Python environment có thể tái tạo
frontend/node_modules/ # Node dependency có thể tái tạo
```

`DATABASE_URL` trong `.env` giữ ở dạng portable:

```env
DATABASE_URL=sqlite:///./data/app.db
```

Không đặt đường dẫn tuyệt đối như `E:\...` trong cấu hình. Launcher tự tạo các thư mục cần thiết. Nếu `.venv` bị hỏng do đã di chuyển dự án, launcher sẽ tự tạo lại nó.

## Xử lý lỗi

| Vấn đề | Cách xử lý |
| --- | --- |
| Không tìm thấy `py`, `node` hoặc `npm` | Cài Python 3.12+ hoặc Node.js 22+, rồi chạy lại `start-app.bat`. |
| Không mở được trang web | Xem `.runtime\logs\api.err.log` và `.runtime\logs\web.err.log`. |
| Quét/tải không chạy | Bảo đảm `start-app.bat` đã chạy; worker ghi log tại `.runtime\logs\worker.err.log`. |
| `blocked` | YouTube có thể giới hạn IP. Giảm `REQUESTS_PER_MINUTE`, chờ rồi thử lại. |
| Không có subtitle | Video có thể không có caption, bị private/removed/age-restricted hoặc tác giả tắt transcript. |

## Phát triển, kiểm thử và CI

Dependency được khóa để cài đặt lặp lại được:

| Phần | File nguồn (sửa tay) | Bản khóa (sinh ra, không sửa tay) |
| --- | --- | --- |
| Backend runtime | `backend/requirements.in` | `backend/requirements.txt` (Python 3.13) |
| Backend dev/CI | `backend/requirements-dev.in` | `backend/requirements-dev.txt` |
| Frontend | `frontend/package.json` (version cố định) | `frontend/package-lock.json` |

`start-app.bat` cài Python theo `requirements.txt` và frontend bằng `npm ci`; dependency chỉ được cài lại khi hash của `requirements.txt` hoặc của `package.json` + `package-lock.json` thay đổi. Docker cũng dùng `pip install -r requirements.txt` và `npm ci`.

Kiểm tra trước khi commit (CI chạy đúng các lệnh này trên GitHub Actions):

```powershell
cd backend
pip install -r requirements-dev.txt
ruff check . ; ruff format --check . ; pytest -q

cd ..\frontend
npm ci
npm run lint ; npm run format:check ; npm run typecheck ; npm test ; npm run build
```

Sửa format tự động: `ruff format .` (backend) và `npm run format` (frontend).

**Cập nhật dependency**: sửa file `.in` (backend) hoặc `package.json`, cài vào môi trường sạch, chạy test, rồi sinh lại bản khóa (`pip freeze` trong venv sạch cho backend; `npm install` cho frontend) và commit cùng nhau.

## Chạy thủ công cho phát triển

Mở ba terminal riêng:

```powershell
# Terminal 1 — API
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000

# Terminal 2 — Worker
cd backend
.\.venv\Scripts\Activate.ps1
python -m app.worker

# Terminal 3 — Web
cd frontend
npm run dev
```

## Ghi chú

- YouTube Data API v3 cấp quota miễn phí mặc định; quota không phải phí tự động.
- API key chỉ dùng để lấy metadata/danh sách video. Caption được lấy bằng `youtube-transcript-api`.
- Không commit `.env`, API key, cookie hoặc credential proxy.
