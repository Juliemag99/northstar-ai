import './Header.css'

interface HeaderProps {
  title: string
  onMenuToggle: () => void
}

export function Header({ title, onMenuToggle }: HeaderProps) {
  return (
    <header className="ns-header">
      <div className="ns-header__left">
        <button
          type="button"
          className="ns-header__menu"
          onClick={onMenuToggle}
          aria-label="Toggle navigation"
        >
          <span />
          <span />
          <span />
        </button>
        <div className="ns-header__title-block">
          <p className="ns-header__eyebrow">Revenue Development Platform</p>
          <h1 className="ns-header__title">{title}</h1>
        </div>
      </div>

      <form className="ns-header__search" role="search" onSubmit={(e) => e.preventDefault()}>
        <label className="sr-only" htmlFor="global-search">
          Search NorthStar AI
        </label>
        <svg className="ns-header__search-icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="11" cy="11" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.75" />
          <path d="M16 16l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
        </svg>
        <input
          id="global-search"
          type="search"
          placeholder="Search prospects, companies, contacts…"
          autoComplete="off"
        />
      </form>

      <div className="ns-header__actions">
        <div className="ns-header__user">
          <div className="ns-header__avatar" aria-hidden="true">
            JM
          </div>
          <div className="ns-header__user-meta">
            <span className="ns-header__user-name">Julie Magnani</span>
            <span className="ns-header__user-role">Revenue Lead</span>
          </div>
        </div>
      </div>
    </header>
  )
}
