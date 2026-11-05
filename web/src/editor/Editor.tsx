import { Link, useParams } from 'react-router-dom';

// Placeholder: the timeline editor is the next work package (web/src/editor/).
export default function Editor() {
  const { id } = useParams();
  return (
    <div className="page">
      <h1>Editor</h1>
      <div className="card">
        <p>Timeline editor coming. Set: <code>{id}</code></p>
        <Link to="/sets" className="btn">Back to sets</Link>
      </div>
    </div>
  );
}
