namespace DotnetOrders.Common;

/// <summary>
/// The shared API/event contract: the OrderCreated message shape.
/// Dialect v2 widens v1 with an optional <c>Region</c>; a v2 consumer
/// accepts both shapes (widening), a v1-only consumer does not.
/// </summary>
public sealed record OrderCreated(string Id, decimal Total, string? Region = null)
{
    public const string DialectV1 = "v1";
    public const string DialectV2 = "v2";

    /// <summary>The dialect this message instance speaks: v2 when the
    /// widening field is present, v1 otherwise.</summary>
    public string Dialect => Region is null ? DialectV1 : DialectV2;
}
