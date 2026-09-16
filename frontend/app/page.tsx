export default function Home() {
  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "0.5rem",
        padding: "1rem",
      }}
    >
      <h1 style={{ margin: 0, fontSize: "2rem" }}>SongForge</h1>
      <p style={{ opacity: 0.7 }}>Scaffold online. The radio starts here.</p>
    </main>
  );
}
