type Props = { offset: number; limit: number; total: number; disabled?: boolean; onChange: (offset: number) => void };

export function Pagination({ offset, limit, total, disabled = false, onChange }: Props) {
  if (total <= limit && offset === 0) return null;
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + limit, total);
  return (
    <nav className="pagination" aria-label="Phân trang">
      <button className="secondary" disabled={disabled || offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        ← Trước
      </button>
      <span>
        {first}–{last} / {total}
      </span>
      <button className="secondary" disabled={disabled || offset + limit >= total} onClick={() => onChange(offset + limit)}>
        Sau →
      </button>
    </nav>
  );
}
