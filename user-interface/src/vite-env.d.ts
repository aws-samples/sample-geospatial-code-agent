/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_COGNITO_USER_POOL_ID: string;
  readonly VITE_COGNITO_CLIENT_ID_STATIC_UI: string;
  readonly VITE_COGNITO_IDENTITY_POOL_ID: string;
  readonly VITE_AGENT_RUNTIME_ARN: string;
  readonly VITE_AWS_REGION: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
