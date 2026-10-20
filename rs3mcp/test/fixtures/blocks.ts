// Known-answer fixture for the comment parser on brace languages.
//
// EXPECT prose units: 6
// EXPECT the wrapped block counts as ONE unit, not three
// EXPECT the commented-out code and the licence header are both discarded

/*
 * Copyright (c) 2020 nobody. Permission is hereby granted, free of charge.
 */

// draw frame
function a() {}

// reset cooldowns
function b() {}

/* input state */
const c = 1;

// Re-read tile bounds each frame because a freshly opened interface reports
// zero by zero until it has laid itself out, which only happens after the
// first draw call completes
const d = 2;

// TODO: no idea why the client returns null here on the first tick
const e = 3;

// Stats (always based on actual GP)
const f = 4;

// const shouldNotCount = 1;
// if (a) { return; }
const g = 5;
