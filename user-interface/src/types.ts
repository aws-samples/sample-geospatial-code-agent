export interface Message {
  role: 'user' | 'assistant';
  content: string;
  images?: string[];
  charts?: Array<{ spec: string; type: string }>;
}

export interface StreamEvent {
  type: 'text' | 'done' | 'error' | 'image' | `python_code` | 'execution_output' | 'result' | 'file_link' | 'interactive_chart';
  content?: string;
  message?: string;
  toolName?: string;
  imageName?: string;
  fileName?: string;
  bounds?: number[][];
  chartType?: string;
}

export interface ImageOverlay {
  image: string;
  bounds: [[number, number], [number, number]];
  imageName?: string;
}

