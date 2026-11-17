# Layout rules

Generic lessons for hand-placed diagrams. Each one exists because the
alternative produced a diagram that looked wrong or misled readers.

1. **Place by hand on a 10 or 20px grid.** Round coordinates make alignment
   obvious in the source: two cards in a column share `left` and `width`; a
   straight edge needs equal centres. Off-grid values hide half-pixel jogs in
   edges.
2. **Make connected centres match.** If `a` is above `b`, give them the same
   centre x (or set `fromAt`/`toAt` so the anchors line up); otherwise the
   edge gets a visible dog-leg.
3. **One stage width per document.** Scaling is `container / data-w`. Mixed
   widths make the same 12.5px text render at different sizes on one page.
4. **Respect the type floor.** Text is scaled with the stage, so a 12px card
   on a 1600px stage shown 800px wide reads as 6px. Keep stages near 1100px
   and fonts at 12px or more; split a busy diagram in two rather than
   shrinking text.
5. **One main flow.** Left to right or top to bottom, the primary path in
   `primary`. Everything that goes back (retry, ack, status) is `feedback`,
   errors are `danger`, side flows `data`. Colour carries the meaning, so do
   not reuse a kind for something else.
6. **Route loops outside.** A return edge from a late box to an early box goes
   around the outside of the whole figure: same side on both ends (e.g.
   `left`/`left` or `bottom`/`bottom`) and an `out` large enough to clear the
   outermost box and its group border. Never let it cut across the middle.
   Keep the outside lane free (margin 20-40px in the stage).
7. **Drop edges that would cross unrelated boxes.** A line through a third
   card reads as a dependency that does not exist. If no clean route exists,
   remove the edge and say so in prose or a `.txt` note ("every service also
   writes to the audit log").
8. **Fan out with `At`.** Several edges leaving one side should use different
   `fromAt` values (0.25 / 0.75) and land on different `toAt` values, so they
   do not merge into one stroke.
9. **Give labels room.** A label is a pill centred on the path midpoint,
   roughly 7px per character plus padding. Leave a gap longer than the label
   between linked cards, or shift it with `dx`/`dy`. Two edges sharing a gap
   need different label positions.
10. **Sequence diagrams: short labels.** A label must fit between its two
    lanes. Split a long self-call into two calls, move detail into a note, or
    widen the stage. Keep message text to the call itself; put explanation in
    prose under the figure.
11. **Groups are captions, not containers of logic.** Draw a group only when
    the boundary means something (a trust zone, a deployment unit); keep a
    20px inset between group border and cards.
12. **Always check a headless render.** The source says where boxes go, but the
    browser decides text wrapping and therefore real heights; labels are
    placed from measured geometry. The verifier estimates; the screenshot is
    the truth. Look at it before declaring the diagram done.
