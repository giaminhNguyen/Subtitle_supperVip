export function ErrorNotice({ message }: { message: string | null | undefined }) {
  return message ? <p className="error" role="alert">{message}</p> : null;
}

export function InfoNotice({ message }: { message: string | null | undefined }) {
  return message ? <p className="notice" role="status">{message}</p> : null;
}
