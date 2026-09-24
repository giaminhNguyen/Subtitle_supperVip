# YouTube Subtitle Manager

Ứng dụng quản lý subtitle hàng loạt từ các kênh YouTube. Dán URL kênh, quét video công khai, đưa việc tải caption vào hàng đợi và lưu subtitle có tổ chức trên máy.

## Chạy nhanh trên Windows

Yêu cầu duy nhất cần cài trước:

- Python 3.12 trở lên, chọn **Add Python to PATH** khi cài.
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
