/** @type {import('next').NextConfig} */
const nextConfig = {
  // session docs carry base64 keyframes; keep server actions/body roomy
  experimental: { serverActions: { bodySizeLimit: "8mb" } },
};

export default nextConfig;
