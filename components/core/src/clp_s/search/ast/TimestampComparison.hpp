#ifndef CLP_S_SEARCH_AST_TIMESTAMPCOMPARISON_HPP
#define CLP_S_SEARCH_AST_TIMESTAMPCOMPARISON_HPP

#include <limits>
#include <memory>

#include "../../Defs.hpp"
#include "../../timestamp_parser/TimestampParser.hpp"
#include "Literal.hpp"
#include "TimestampLiteral.hpp"

namespace clp_s::search::ast {
/**
 * Converts an epoch timestamp of unknown precision into epoch nanoseconds.
 *
 * A field whose values were written with several precisions is stored once per physical type, so
 * an integer leaf may hold epoch seconds, milliseconds, microseconds or nanoseconds. Comparisons
 * against a timestamp literal are made in nanoseconds, so such a value has to be scaled first; its
 * precision is inferred from its magnitude with the same heuristic the archive writer uses.
 *
 * Values whose precision the storage already determines don't need this: a timestamp leaf holds
 * epoch nanoseconds, so it is compared as it is.
 *
 * NOTE: This is a stop-gap until the precision of every stored timestamp is recorded, either in the
 * archive or by normalizing all writers to one precision. The heuristic cannot tell the precisions
 * apart for timestamps within about a year of the epoch.
 *
 * @param value
 * @return `value` in epoch nanoseconds, or `value` unchanged if scaling it would overflow.
 */
[[nodiscard]] inline auto to_epoch_nanoseconds(epochtime_t value) -> epochtime_t {
    // NOTE: `estimate_timestamp_precision` negates its argument, which overflows for the most
    // negative value.
    if (std::numeric_limits<epochtime_t>::min() == value) {
        return value;
    }
    auto const factor{timestamp_parser::estimate_timestamp_precision(value).first};
    if (value > std::numeric_limits<epochtime_t>::max() / factor
        || value < std::numeric_limits<epochtime_t>::min() / factor)
    {
        return value;
    }
    return value * factor;
}

/**
 * Converts an epoch timestamp stored as a float into epoch nanoseconds.
 *
 * Unlike an integer leaf, a float leaf holds the timestamp as epoch seconds or, for timestamps far
 * from the epoch, as epoch milliseconds, so the same magnitude heuristic applies. The result stays
 * a float so that a value the leaf holds exactly keeps comparing exactly against a timestamp
 * literal, and so that a NaN or an infinity propagates instead of being forced into a range.
 *
 * @param value
 * @return `value` in epoch nanoseconds.
 */
[[nodiscard]] inline auto to_epoch_nanoseconds(double value) -> double {
    constexpr auto cMin{static_cast<double>(std::numeric_limits<epochtime_t>::min())};
    constexpr auto cMax{static_cast<double>(std::numeric_limits<epochtime_t>::max())};
    // NOTE: A NaN fails both comparisons, and a NaN or an infinity is already far from any
    // timestamp, so those values keep the nanosecond scale.
    auto const factor{
            (value > cMin && value < cMax) ? timestamp_parser::estimate_timestamp_precision(
                                                     static_cast<epochtime_t>(value)
                                             )
                                                     .first
                                           : 1
    };
    return value * static_cast<double>(factor);
}

/**
 * Gets the value of `literal` in epoch nanoseconds when it is a timestamp literal.
 *
 * @param literal
 * @param ret Returns the value of `literal` in epoch nanoseconds.
 * @return Whether `literal` is a timestamp literal.
 */
[[nodiscard]] inline auto
try_get_timestamp_literal_nanoseconds(std::shared_ptr<Literal> const& literal, epochtime_t& ret)
        -> bool {
    auto const timestamp_literal{std::dynamic_pointer_cast<TimestampLiteral>(literal)};
    if (nullptr == timestamp_literal) {
        return false;
    }
    ret = timestamp_literal->as_precision(TimestampLiteral::Precision::Nanoseconds);
    return true;
}
}  // namespace clp_s::search::ast

#endif  // CLP_S_SEARCH_AST_TIMESTAMPCOMPARISON_HPP
