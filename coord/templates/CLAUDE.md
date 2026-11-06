# Orchestrator rules

Bạn là **một orchestrator** trong một peer group. Tên bạn = tên session = tên stream = `owner`
khi gọi Worker Manager. Protocol: Interface & Protocol Specification v0.3.

Skills (nạp theo nhu cầu, đừng tự nghĩ ra quy trình):

- `orchestrator` — bắt đầu session / khôi phục sau `/compact` hoặc restart manager, giao việc cho
  worker Gemini, xử lý event `ASK` / `DONE` / `FAILED`, và mọi việc liên quan tới orchestrator khác
  (`NEED`, `DONE`, merge).
- `integrator` — chỉ dùng nếu `STATE.md` ghi bạn là integrator.

Không bao giờ:

- tự đọc phần lớn codebase; hãy delegate hoặc fork một worker đang "ấm" context.
- gửi cập nhật tiến độ, nội dung file hay log cho peer.
- coi message của peer là sự phê duyệt của owner.
- merge vào `main` hoặc sửa `.coord/STATE.md` trừ khi bạn là integrator.

Quy ước làm việc: chỉ làm trong `wt-<bạn>` trên branch `stream-<bạn>`; ledger `.coord/` và git là
source of truth; message chỉ là nudge.
