import React, { useEffect, useMemo, useRef, useState } from 'react'
import { items } from './data.js'

const normalize = (s) => (s || '').toString().toLowerCase().trim()

const scoreMatch = (q, text) => {
  if (!q) return 0
  const t = normalize(text)
  const parts = normalize(q).split(/\s+/).filter(Boolean)
  let score = 0
  for (const p of parts) {
    if (!p) continue
    let idx = t.indexOf(p)
    while (idx !== -1) {
      score += 1
      idx = t.indexOf(p, idx + p.length)
    }
  }
  return score
}

const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const Highlight = ({ query, text }) => {
  const q = normalize(query)
  const raw = text || ''
  if (!q) return <>{raw}</>
  const parts = q.split(/\s+/).filter(Boolean).map(escapeRegExp)
  if (!parts.length) return <>{raw}</>
  const re = new RegExp(`(${parts.join('|')})`, 'ig')
  const segs = raw.split(re)
  return (
    <>
      {segs.map((seg, i) => {
        const isHit = re.test(seg)
        re.lastIndex = 0
        return isHit ? <mark key={i}>{seg}</mark> : <React.Fragment key={i}>{seg}</React.Fragment>
      })}
    </>
  )
}

const uniq = (arr) => Array.from(new Set(arr))

export default function App() {
  const inputRef = useRef(null)
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('Все')
  const [sort, setSort] = useState('Релевантность')
  const [recent, setRecent] = useState(() => {
    try {
      const raw = localStorage.getItem('recent_searches_v1')
      const parsed = raw ? JSON.parse(raw) : []
      return Array.isArray(parsed) ? parsed.slice(0, 8) : []
    } catch {
      return []
    }
  })

  const categories = useMemo(() => ['Все', ...uniq(items.map((x) => x.category)).sort((a, b) => a.localeCompare(b, 'ru'))], [])

  const computed = useMemo(() => {
    const q = normalize(query)
    const selected = category === 'Все' ? items : items.filter((x) => x.category === category)

    const rows = selected
      .map((x) => {
        const hay = [x.title, x.text, (x.tags || []).join(' ')].join(' ')
        const s = scoreMatch(q, hay)
        const ok = !q || s > 0 || normalize(hay).includes(q)
        return { ...x, _score: s, _ok: ok }
      })
      .filter((x) => x._ok)

    if (sort === 'А-Я') rows.sort((a, b) => a.title.localeCompare(b.title, 'ru'))
    if (sort === 'Я-А') rows.sort((a, b) => b.title.localeCompare(a.title, 'ru'))
    if (sort === 'Релевантность') rows.sort((a, b) => (b._score - a._score) || a.title.localeCompare(b.title, 'ru'))

    return rows
  }, [query, category, sort])

  useEffect(() => {
    const onKeyDown = (e) => {
      if (e.key === '/' && !(e.ctrlKey || e.metaKey || e.altKey)) {
        e.preventDefault()
        inputRef.current?.focus()
      }
      if (e.key === 'Escape') {
        if (document.activeElement === inputRef.current) inputRef.current?.blur()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const saveRecent = (value) => {
    const v = (value || '').trim()
    if (!v) return
    const next = [v, ...recent.filter((x) => x !== v)].slice(0, 8)
    setRecent(next)
    try {
      localStorage.setItem('recent_searches_v1', JSON.stringify(next))
    } catch {}
  }

  const onSubmit = (e) => {
    e.preventDefault()
    saveRecent(query)
  }

  const onPickRecent = (v) => {
    setQuery(v)
    inputRef.current?.focus()
  }

  const onClearRecent = () => {
    setRecent([])
    try {
      localStorage.removeItem('recent_searches_v1')
    } catch {}
  }

  const hasRecent = recent.length > 0

  return (
    <div className="page">
      <header className="top">
        <div className="brand">
          <div className="logo" aria-hidden="true">S</div>
          <div className="brandText">
            <div className="title">Одностраничный поиск</div>
            <div className="subtitle">Нажми “/”, чтобы быстро перейти к поиску</div>
          </div>
        </div>

        <form className="search" onSubmit={onSubmit}>
          <label className="field">
            <span className="label">Запрос</span>
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Например: аудит, тендеры, чеклист…"
              spellCheck={false}
              autoComplete="off"
              inputMode="search"
            />
          </label>

          <label className="field">
            <span className="label">Категория</span>
            <select value={category} onChange={(e) => setCategory(e.target.value)}>
              {categories.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>

          <label className="field">
            <span className="label">Сортировка</span>
            <select value={sort} onChange={(e) => setSort(e.target.value)}>
              <option value="Релевантность">Релевантность</option>
              <option value="А-Я">А-Я</option>
              <option value="Я-А">Я-А</option>
            </select>
          </label>

          <div className="actions">
            <button type="button" className="btn ghost" onClick={() => setQuery('')} disabled={!query}>
              Очистить
            </button>
            <button type="submit" className="btn" disabled={!query.trim()}>
              Сохранить запрос
            </button>
          </div>
        </form>
      </header>

      <main className="content">
        <section className="panel">
          <div className="meta">
            <div className="count">Найдено: <b>{computed.length}</b></div>
            <div className="hint">Поиск идёт по названию, описанию и тегам</div>
          </div>

          {hasRecent && (
            <div className="recent">
              <div className="recentHead">
                <div className="recentTitle">Недавние запросы</div>
                <button className="link" type="button" onClick={onClearRecent}>очистить</button>
              </div>
              <div className="chips">
                {recent.map((r) => (
                  <button key={r} type="button" className="chip" onClick={() => onPickRecent(r)}>
                    {r}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="grid">
            {computed.map((x) => (
              <article key={x.id} className="card">
                <div className="cardTop">
                  <div className="badge">{x.category}</div>
                </div>
                <h3 className="cardTitle">
                  <Highlight query={query} text={x.title} />
                </h3>
                <p className="cardText">
                  <Highlight query={query} text={x.text} />
                </p>
                <div className="tags">
                  {(x.tags || []).map((t) => (
                    <span key={t} className="tag">{t}</span>
                  ))}
                </div>
              </article>
            ))}
          </div>

          {computed.length === 0 && (
            <div className="empty">
              <div className="emptyTitle">Ничего не найдено</div>
              <div className="emptyText">Попробуй другой запрос или выбери категорию “Все”.</div>
            </div>
          )}
        </section>
      </main>

      <footer className="footer">
        <div>Фронт без бэка · Vite + React</div>
        <div className="footerRight">
          <span className="kbd">/</span> фокус
          <span className="sep">·</span>
          <span className="kbd">Esc</span> blur
        </div>
      </footer>
    </div>
  )
}
