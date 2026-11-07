interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_BASEMAP_DARK?: string;
  readonly VITE_BASEMAP_LIGHT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
