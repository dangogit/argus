import "./globals.css";

export const metadata = {
  title: "Argus",
  description: "Read-only console for the Argus monitoring crew",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        <header className="argus-header">
          <h1>Argus</h1>
          <span className="tagline">read-only console</span>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
