import { useEffect, useState } from "react";
import { getPolicy } from "../api";

export default function PolicyView() {
  const [text, setText] = useState<string>("");
  useEffect(() => {
    getPolicy().then(setText).catch((e) => setText(String(e)));
  }, []);
  return <pre className="policy-pre">{text || "Loading policy…"}</pre>;
}
