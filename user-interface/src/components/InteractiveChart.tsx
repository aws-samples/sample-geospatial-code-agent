import { lazy, Suspense } from 'react';

const Plot = lazy(() => import('react-plotly.js'));

interface InteractiveChartProps {
  spec: string;
  type: string;
}

export function InteractiveChart({ spec, type }: InteractiveChartProps) {
  if (type !== 'plotly') {
    return <div>Unsupported chart type: {type}</div>;
  }

  try {
    const figure = JSON.parse(spec);
    return (
      <Suspense fallback={<div>Loading chart...</div>}>
        <Plot
          data={figure.data}
          layout={{
            ...figure.layout,
            autosize: true,
            margin: { l: 40, r: 20, t: 40, b: 40 },
          }}
          config={{ responsive: true, displayModeBar: true }}
          style={{ width: '100%', minHeight: '350px' }}
        />
      </Suspense>
    );
  } catch {
    return <div>Failed to parse chart specification</div>;
  }
}
