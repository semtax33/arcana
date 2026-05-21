import { observer } from "mobx-react-lite";
import { Sidebar } from "./components/Sidebar";
import { Workspace } from "./components/Workspace";
import { quantScreenerStore } from "./stores/quantScreenerStore";
import { screenerStore } from "./stores/screenerStore";

const App = observer(() => {
  return (
    <main className="app-shell">
      <Sidebar store={screenerStore} />
      <Workspace screenerStore={screenerStore} quantStore={quantScreenerStore} />
    </main>
  );
});

export default App;
