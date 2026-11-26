import { Component } from "react";

/** Keeps a rendering error inside one area of the page (no stack traces shown). */
export class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidUpdate(previous) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="card" role="alert" style={{ padding: 20 }}>
        <strong>This section could not be displayed.</strong>
        <p className="muted small">Something went wrong while showing this page. Reload the page or open another section.</p>
        <button type="button" className="btn btn-sm" onClick={() => this.setState({ failed: false })}>
          Try again
        </button>
      </div>
    );
  }
}
