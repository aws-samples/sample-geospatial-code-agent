import { Component, type ReactNode, useEffect, useRef, useState } from 'react';

/**
 * plotly.js ships a single ~5MB prebuilt bundle. Importing it through Vite's
 * module graph makes Vite's dep optimizer feed that giant file to the
 * es-module-lexer (WASM), which overflows its memory during the dep scan
 * ("WebAssembly.Memory.grow(): Maximum memory size exceeded") and fails
 * import-analysis ("invalid JS syntax"). No optimizeDeps include/exclude
 * combination avoids this, because plotly is CommonJS and must be bundled
 * into one ESM module to be imported.
 *
 * Instead we load the prebuilt bundle from /public via a <script> tag. Files
 * in public/ are served as-is and never pass through the optimizer or lexer,
 * so the whole problem is bypassed. Plotly attaches itself to window.Plotly.
 */
const PLOTLY_SRC = '/plotly.min.js';

interface PlotlyApi {
  newPlot(root: HTMLElement, data: unknown[], layout?: unknown, config?: unknown): Promise<unknown>;
  purge(root: HTMLElement): void;
}

declare global {
  interface Window {
    Plotly?: PlotlyApi;
  }
}

let plotlyPromise: Promise<PlotlyApi> | null = null;

function loadPlotly(): Promise<PlotlyApi> {
  if (window.Plotly) return Promise.resolve(window.Plotly);
  if (plotlyPromise) return plotlyPromise;

  plotlyPromise = new Promise<PlotlyApi>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${PLOTLY_SRC}"]`);
    const onLoad = () => {
      if (window.Plotly) resolve(window.Plotly);
      else reject(new Error('Plotly failed to initialize'));
    };
    if (existing) {
      existing.addEventListener('load', onLoad, { once: true });
      existing.addEventListener('error', () => reject(new Error('Failed to load Plotly')), { once: true });
      return;
    }
    const script = document.createElement('script');
    script.src = PLOTLY_SRC;
    script.async = true;
    script.addEventListener('load', onLoad, { once: true });
    script.addEventListener('error', () => reject(new Error('Failed to load Plotly')), { once: true });
    document.head.appendChild(script);
  }).catch((e) => {
    // allow a later retry if loading failed
    plotlyPromise = null;
    throw e;
  });

  return plotlyPromise;
}

class ChartErrorBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(error: Error) { return { error: error.message }; }
  render() {
    if (this.state.error) return <div>Chart rendering error: {this.state.error}</div>;
    return this.props.children;
  }
}

interface InteractiveChartProps {
  spec: string;
  type: string;
}

function PlotlyChart({ spec }: { spec: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    let cancelled = false;

    loadPlotly().then((Plotly) => {
      if (cancelled || !containerRef.current) return;
      try {
        const figure = JSON.parse(spec);
        Plotly.newPlot(containerRef.current, figure.data, {
          ...figure.layout,
          autosize: true,
          margin: { l: 40, r: 20, t: 40, b: 40 },
        }, { responsive: true, displayModeBar: true });
      } catch (e: any) {
        setError(e.message);
      }
    }).catch((e: any) => setError(e.message));

    return () => {
      cancelled = true;
      const node = containerRef.current;
      if (node && window.Plotly) window.Plotly.purge(node);
    };
  }, [spec]);

  if (error) return <div>Chart error: {error}</div>;
  return <div ref={containerRef} style={{ width: '100%', minHeight: '350px' }} />;
}

export function InteractiveChart({ spec, type }: InteractiveChartProps) {
  if (type !== 'plotly') {
    return <div>Unsupported chart type: {type}</div>;
  }

  return (
    <ChartErrorBoundary>
      <PlotlyChart spec={spec} />
    </ChartErrorBoundary>
  );
}
