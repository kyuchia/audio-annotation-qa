"""
The registered annotation types.

Adding one means a single register_layer_type() call here. Models, serializers and admin
all stay untouched, which is the property this registry exists to provide.

note_pitch is the type the demo corpus uses. Sound event detection and audio tagging are
registered alongside it to keep that property honest: both are storable as they stand,
without changing a model, and a second and third type in the table is what demonstrates
the first one has not quietly become the schema.
"""
from .registry import LayerTypeSpec, PayloadField, register_layer_type

NOTE_PITCH = register_layer_type(
    LayerTypeSpec(
        key="note_pitch",
        label="Note pitch (time interval plus pitch)",
        segment_scope="interval",
        payload_fields=(
            PayloadField(
                "hz",
                "number",
                required=True,
                minimum=20.0,
                maximum=8000.0,
                help_text="Stored in Hz, never as a MIDI note. MIDI can only hold whole "
                "semitones, which makes it a derived view.",
            ),
        ),
        param_fields=(
            PayloadField(
                "tuning_reference_hz",
                "number",
                required=False,
                minimum=380.0,
                maximum=500.0,
                help_text="The A4 reference for this layer. Set it per layer when the corpus "
                "is not tuned to 440; pitch comparison falls back to 440 without it.",
            ),
        ),
        default_comparator="note_pitch_interval_cents",
        validators=(
            "segments_monotonic",
            "annotation_coverage",
            "annotation_internal_gaps",
            "media_probed",
            "metadata_duration_agreement",
        ),
        # The shipped thresholds in validators/checks.py are sized for whole pieces of
        # several minutes. These excerpts run 10–40 seconds, where a 5s tail gap is a
        # fifth of the file and a 10s internal gap cannot occur at all, so both checks
        # would be silent by construction rather than by agreement with the data.
        # Measured across all 80 vocadito note layers: tail gaps run 0.0–2.25s (median
        # 0.32), and the longest internal gap is 4.23s (median 0.62). The values below sit
        # just above the bulk of each distribution, so they report the outliers and stay
        # quiet on the rest.
        #
        # The overlap tolerance is a different kind of number. A note source states an
        # onset and a duration, so a segment's end time is a sum of two independently
        # rounded values: a note can appear to run microseconds past the next onset with
        # nothing wrong in the annotation. 1 ms is far above the rounding these files
        # actually carry (the largest such overlap in the demo corpus is 2.3e-05s) and far
        # below any overlap a person could annotate by hand, so the two cases stay on
        # opposite sides of it. An overlap within it is still reported, as info.
        validator_params={
            "annotation_coverage": {"coverage_gap_sec": 2.0},
            "annotation_internal_gaps": {"internal_gap_sec": 3.0},
            "segments_monotonic": {"segment_overlap_tolerance_sec": 0.001},
        },
        description=(
            "(onset, offset, hz). A comparison must handle both the interval (IoU or onset "
            "tolerance) and the pitch (a cents tolerance — never compare Hz directly)."
        ),
    )
)

SOUND_EVENT = register_layer_type(
    LayerTypeSpec(
        key="sound_event",
        label="Sound event detection (time interval plus label)",
        segment_scope="interval",
        payload_fields=(
            PayloadField("label", "string", required=True),
            PayloadField("confidence", "number", required=False, minimum=0.0, maximum=1.0),
        ),
        default_comparator="interval_label_iou",
        validators=("segments_monotonic", "annotation_coverage", "annotation_internal_gaps"),
    )
)

AUDIO_TAGGING = register_layer_type(
    LayerTypeSpec(
        key="audio_tagging",
        label="Audio tagging (clip-level labels)",
        segment_scope="clip",
        payload_fields=(PayloadField("labels", "list", required=True),),
        default_comparator=None,
        validators=("clip_single_segment",),
        description="Clip level: a single segment covering the whole file, with a labels "
        "array in the payload.",
    )
)
