import { useEffect, useState } from "react";

type Contact = { id: string; name: string; phone: string };
type CaseInfo = { id: string; rc: string; rc_description: string; failure_reason: string };
type CallResult = {
  channel: "voice" | "whatsapp";
  callSid?: string;
  messageSid?: string;
  contact: Contact;
  case: CaseInfo;
  preview?: string;
};
type Channel = "voice" | "whatsapp";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export default function App() {
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [channel, setChannel] = useState<Channel>("voice");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CallResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const r = await fetch(`${API_BASE}/api/contacts`);
        if (!r.ok) throw new Error(`Server returned ${r.status} ${r.statusText}`);
        const text = await r.text();
        if (!text) throw new Error("Empty response from /api/contacts — is the FastAPI server running on :8000?");
        setContacts(JSON.parse(text));
      } catch (e: any) {
        setError(e.message || String(e));
      }
    })();
  }, []);

  const initiate = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch(`${API_BASE}/api/call`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contactId: selected, channel }),
      });
      if (!res.ok) throw new Error(await res.text());
      setResult(await res.json());
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const ctaLabel = channel === "voice" ? "חייג ללקוח" : "שלח הודעת וואטסאפ";
  const busyLabel = channel === "voice" ? "מחייג…" : "שולח…";

  return (
    <div className="app">
      <div className="header">
        <div className="logo">PC</div>
        <div>
          <div className="title">PassportCard Pay – Service Agent Demo</div>
          <div className="subtitle">סוכן חכם שפונה ללקוח אחרי סירוב עסקה — בקול או בוואטסאפ</div>
        </div>
      </div>

      <div className="card">
        <h3 className="section-title">בחירת לקוח</h3>
        <div className="contact-grid">
          {contacts.map((c) => (
            <div
              key={c.id}
              className={`contact ${selected === c.id ? "selected" : ""}`}
              onClick={() => setSelected(c.id)}
            >
              <div className="name">{c.name}</div>
              <div className="phone">{c.phone || "— מספר טרם הוגדר —"}</div>
              {!c.phone && <span className="badge">חסר מספר ב-contacts.json</span>}
            </div>
          ))}
        </div>

        <h3 className="section-title">ערוץ פנייה</h3>
        <div className="channel-toggle" role="tablist">
          <button
            className={`channel ${channel === "voice" ? "active" : ""}`}
            onClick={() => setChannel("voice")}
            type="button"
          >
            <span className="ico">📞</span>
            שיחה קולית
          </button>
          <button
            className={`channel ${channel === "whatsapp" ? "active" : ""}`}
            onClick={() => setChannel("whatsapp")}
            type="button"
          >
            <span className="ico">💬</span>
            וואטסאפ
          </button>
        </div>

        <div className="cta-row">
          <button className="btn" disabled={!selected || busy} onClick={initiate}>
            <span className="ico">{channel === "voice" ? "📞" : "💬"}</span>
            {busy ? busyLabel : ctaLabel}
          </button>
          {busy && (
            <span>
              <span className="pulse" />
              {channel === "voice" ? "יוזם שיחה דרך Abra…" : "שולח הודעה דרך Abra…"}
            </span>
          )}
        </div>

        {error && (
          <div className="status error">
            <div className="label">שגיאה</div>
            <div>{error}</div>
          </div>
        )}

        {result && (
          <div className="status">
            <div className="label">
              {result.channel === "voice" ? "השיחה הופעלה" : "הודעת וואטסאפ נשלחה"}
            </div>
            <div className="row">
              <span>{result.channel === "voice" ? "מתקשרים אל" : "נשלח אל"}</span>
              <strong>{result.contact.name}</strong>
              <span>·</span>
              <code>{result.contact.phone}</code>
            </div>
            <div className="row" style={{ marginTop: 4 }}>
              <span>{result.channel === "voice" ? "Call SID:" : "Message SID:"}</span>
              <code>{result.callSid || result.messageSid}</code>
            </div>

            <div className="case-box">
              <div className="label">תרחיש שנבחר אקראית</div>
              <div className="reason">{result.case.failure_reason}</div>
              <div className="rc">RC {result.case.rc} — {result.case.rc_description}</div>
            </div>

            {result.preview && (
              <div className="case-box" style={{ whiteSpace: "pre-wrap" }}>
                <div className="label">תוכן ההודעה</div>
                <div style={{ marginTop: 8 }}>{result.preview}</div>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="footer">© PassportCard demo · Abra Amit (Voice & WhatsApp)</div>
    </div>
  );
}
