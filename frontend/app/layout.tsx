import type { Metadata } from "next";
import { ToastProvider } from "@/lib/useToast";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Router",
  description: "Multi-provider AI chat router",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}
